# Copyright (c) 2021 PaddlePaddle Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import logging
import time
from collections import defaultdict
from pathlib import Path

import numpy as np
import paddle
import paddle.nn as nn
import pandas as pd
from paddle import distributed as dist
from paddle.io import DataLoader
from sklearn.metrics import classification_report
from sklearn.metrics import f1_score
from sklearn.metrics import precision_recall_fscore_support
from speechtask.punctuation_restoration.io.dataset import PuncDataset
from speechtask.punctuation_restoration.io.dataset import PuncDatasetFromBertTokenizer
from speechtask.punctuation_restoration.model.BertBLSTM import BertBLSTMPunc
from speechtask.punctuation_restoration.model.BertLinear import BertLinearPunc
from speechtask.punctuation_restoration.model.blstm import BiLSTM
from speechtask.punctuation_restoration.model.lstm import RnnLm
from speechtask.punctuation_restoration.utils import layer_tools
from speechtask.punctuation_restoration.utils import mp_tools
from speechtask.punctuation_restoration.utils.checkpoint import Checkpoint
from visualdl import LogWriter

__all__ = ["Trainer", "Tester"]

DefinedClassifier = {
    "lstm": RnnLm,
    "blstm": BiLSTM,
    "BertLinear": BertLinearPunc,
    "BertBLSTM": BertBLSTMPunc
}

DefinedLoss = {
    "ce": nn.CrossEntropyLoss,
}

DefinedDataset = {
    'PuncCh': PuncDataset,
    'Bert': PuncDatasetFromBertTokenizer,
}


class Trainer():
    """
    An experiment template in order to structure the training code and take 
    care of saving, loading, logging, visualization stuffs. It"s intended to 
    be flexible and simple. 
    
    So it only handles output directory (create directory for the output, 
    create a checkpoint directory, dump the config in use and create 
    visualizer and logger) in a standard way without enforcing any
    input-output protocols to the model and dataloader. It leaves the main 
    part for the user to implement their own (setup the model, criterion, 
    optimizer, define a training step, define a validation function and 
    customize all the text and visual logs).
    It does not save too much boilerplate code. The users still have to write 
    the forward/backward/update mannually, but they are free to add 
    non-standard behaviors if needed.
    We have some conventions to follow.
    1. Experiment should have ``model``, ``optimizer``, ``train_loader`` and 
    ``valid_loader``, ``config`` and ``args`` attributes.
    2. The config should have a ``training`` field, which has 
    ``valid_interval``, ``save_interval`` and ``max_iteration`` keys. It is 
    used as the trigger to invoke validation, checkpointing and stop of the 
    experiment.
    3. There are four methods, namely ``train_batch``, ``valid``, 
    ``setup_model`` and ``setup_dataloader`` that should be implemented.
    Feel free to add/overwrite other methods and standalone functions if you 
    need.
    
    Parameters
    ----------
    config: yacs.config.CfgNode
        The configuration used for the experiment.
    
    args: argparse.Namespace
        The parsed command line arguments.
    Examples
    --------
    >>> def main_sp(config, args):
    >>>     exp = Trainer(config, args)
    >>>     exp.setup()
    >>>     exp.run()
    >>> 
    >>> config = get_cfg_defaults()
    >>> parser = default_argument_parser()
    >>> args = parser.parse_args()
    >>> if args.config: 
    >>>     config.merge_from_file(args.config)
    >>> if args.opts:
    >>>     config.merge_from_list(args.opts)
    >>> config.freeze()
    >>> 
    >>> if args.ngpu > 1:
    >>>     dist.spawn(main_sp, args=(config, args), nprocs=args.ngpu)
    >>> else:
    >>>     main_sp(config, args)
    """

    def __init__(self, config, args):
        self.config = config
        self.args = args
        self.optimizer = None
        self.visualizer = None
        self.output_dir = None
        self.checkpoint_dir = None
        self.iteration = 0
        self.epoch = 0

    def setup(self):
        """Setup the experiment.
        """
        self.setup_logger()
        paddle.set_device(self.args.device)
        if self.parallel:
            self.init_parallel()

        self.setup_output_dir()
        self.dump_config()
        self.setup_visualizer()
        self.setup_checkpointer()

        self.setup_model()

        self.setup_dataloader()

        self.iteration = 0
        self.epoch = 0

    @property
    def parallel(self):
        """A flag indicating whether the experiment should run with 
        multiprocessing.
        """
        return self.args.ngpu > 1

    def init_parallel(self):
        """Init environment for multiprocess training.
        """
        dist.init_parallel_env()

    @mp_tools.rank_zero_only
    def save(self, tag=None, infos: dict=None):
        """Save checkpoint (model parameters and optimizer states).

        Args:
            tag (int or str, optional): None for step, else using tag, e.g epoch. Defaults to None.
            infos (dict, optional): meta data to save. Defaults to None.
        """

        infos = infos if infos else dict()
        infos.update({
            "step": self.iteration,
            "epoch": self.epoch,
            "lr": self.optimizer.get_lr()
        })
        self.checkpointer.add_checkpoint(self.checkpoint_dir, self.iteration
                                         if tag is None else tag, self.model,
                                         self.optimizer, infos)

    def resume_or_scratch(self):
        """Resume from latest checkpoint at checkpoints in the output 
        directory or load a specified checkpoint.
        
        If ``args.checkpoint_path`` is not None, load the checkpoint, else
        resume training.
        """
        scratch = None
        infos = self.checkpointer.load_parameters(
            self.model,
            self.optimizer,
            checkpoint_dir=self.checkpoint_dir,
            checkpoint_path=self.args.checkpoint_path)
        if infos:
            # restore from ckpt
            self.iteration = infos["step"]
            self.epoch = infos["epoch"]
            scratch = False
        else:
            self.iteration = 0
            self.epoch = 0
            scratch = True

        return scratch

    def new_epoch(self):
        """Reset the train loader seed and increment `epoch`.
        """
        self.epoch += 1
        if self.parallel:
            self.train_loader.batch_sampler.set_epoch(self.epoch)

    def train(self):
        """The training process control by epoch."""
        from_scratch = self.resume_or_scratch()

        if from_scratch:
            # save init model, i.e. 0 epoch
            self.save(tag="init")

        self.lr_scheduler.step(self.iteration)
        if self.parallel:
            self.train_loader.batch_sampler.set_epoch(self.epoch)

        self.logger.info(
            f"Train Total Examples: {len(self.train_loader.dataset)}")
        self.punc_list = []
        for i in range(len(self.train_loader.dataset.id2punc)):
            self.punc_list.append(self.train_loader.dataset.id2punc[i])
        while self.epoch < self.config["training"]["n_epoch"]:
            self.model.train()
            self.total_label_train = []
            self.total_predict_train = []
            try:
                data_start_time = time.time()
                for batch_index, batch in enumerate(self.train_loader):
                    dataload_time = time.time() - data_start_time
                    msg = "Train: Rank: {}, ".format(dist.get_rank())
                    msg += "epoch: {}, ".format(self.epoch)
                    msg += "step: {}, ".format(self.iteration)
                    msg += "batch : {}/{}, ".format(batch_index + 1,
                                                    len(self.train_loader))
                    msg += "lr: {:>.8f}, ".format(self.lr_scheduler())
                    msg += "data time: {:>.3f}s, ".format(dataload_time)
                    self.train_batch(batch_index, batch, msg)
                    data_start_time = time.time()
                t = classification_report(
                    self.total_label_train,
                    self.total_predict_train,
                    target_names=self.punc_list)
                self.logger.info(t)
            except Exception as e:
                self.logger.error(e)
                raise e

            total_loss, F1_score = self.valid()
            self.logger.info("Epoch {} Val info val_loss {}, F1_score {}".
                             format(self.epoch, total_loss, F1_score))
            if self.visualizer:
                self.visualizer.add_scalar(
                    tag='eval/cv_loss', value=cv_loss, step=self.epoch)
                self.visualizer.add_scalar(
                    tag='eval/lr', value=self.lr_scheduler(), step=self.epoch)

            self.save(
                tag=self.epoch, infos={"val_loss": total_loss,
                                       "F1": F1_score})
            # step lr every epoch
            self.lr_scheduler.step()
            self.new_epoch()

    def run(self):
        """The routine of the experiment after setup. This method is intended
        to be used by the user.
        """
        try:
            self.train()
        except KeyboardInterrupt:
            self.save()
            exit(-1)
        finally:
            self.destory()
        self.logger.info("Training Done.")

    def setup_output_dir(self):
        """Create a directory used for output.
        """
        # output dir
        output_dir = Path(self.args.output).expanduser()
        output_dir.mkdir(parents=True, exist_ok=True)

        self.output_dir = output_dir

    def setup_checkpointer(self):
        """Create a directory used to save checkpoints into.
        
        It is "checkpoints" inside the output directory.
        """
        # checkpoint dir
        self.checkpointer = Checkpoint(self.logger,
                                       self.config["checkpoint"]["kbest_n"],
                                       self.config["checkpoint"]["latest_n"])

        checkpoint_dir = self.output_dir / "checkpoints"
        checkpoint_dir.mkdir(exist_ok=True)

        self.checkpoint_dir = checkpoint_dir

    def setup_logger(self):
        LOG_FORMAT = "%(asctime)s - %(pathname)s[line:%(lineno)d] - %(levelname)s: %(message)s"
        format_str = logging.Formatter(
            '%(asctime)s - %(pathname)s[line:%(lineno)d] - %(levelname)s: %(message)s'
        )
        logging.basicConfig(
            filename=self.config["training"]["log_path"],
            level=logging.INFO,
            format=LOG_FORMAT)
        self.logger = logging.getLogger(__name__)
        # self.logger = logging.getLogger(self.config["training"]["log_path"].strip().split('/')[-1].split('.')[0])

        self.logger.setLevel(logging.INFO)  #设置日志级别
        sh = logging.StreamHandler()  #往屏幕上输出
        sh.setFormatter(format_str)  #设置屏幕上显示的格式
        self.logger.addHandler(sh)  #把对象加到logger里

        self.logger.info('info')
        print("setup logger!!!")

    @mp_tools.rank_zero_only
    def destory(self):
        """Close visualizer to avoid hanging after training"""
        # https://github.com/pytorch/fairseq/issues/2357
        if self.visualizer:
            self.visualizer.close()

    @mp_tools.rank_zero_only
    def setup_visualizer(self):
        """Initialize a visualizer to log the experiment.
        
        The visual log is saved in the output directory.
        
        Notes
        ------
        Only the main process has a visualizer with it. Use multiple 
        visualizers in multiprocess to write to a same log file may cause 
        unexpected behaviors.
        """
        # visualizer
        visualizer = LogWriter(logdir=str(self.output_dir))
        self.visualizer = visualizer

    @mp_tools.rank_zero_only
    def dump_config(self):
        """Save the configuration used for this experiment. 
        
        It is saved in to ``config.yaml`` in the output directory at the 
        beginning of the experiment.
        """
        with open(self.output_dir / "config.yaml", "wt") as f:
            print(self.config, file=f)

    def train_batch(self, batch_index, batch_data, msg):
        start = time.time()

        input, label = batch_data
        label = paddle.reshape(label, shape=[-1])
        y, logit = self.model(input)
        pred = paddle.argmax(logit, axis=1)
        self.total_label_train.extend(label.numpy().tolist())
        self.total_predict_train.extend(pred.numpy().tolist())
        # self.total_predict.append(logit.numpy().tolist())
        # print('--after model----')
        # # print(label.shape)
        # # print(pred.shape)
        # # print('--!!!!!!!!!!!!!----')
        # print("self.total_label")
        # print(self.total_label)
        # print("self.total_predict")
        # print(self.total_predict)
        loss = self.crit(y, label)

        loss.backward()
        layer_tools.print_grads(self.model, print_func=None)
        self.optimizer.step()
        self.optimizer.clear_grad()
        iteration_time = time.time() - start

        losses_np = {
            "train_loss": float(loss),
        }
        msg += "train time: {:>.3f}s, ".format(iteration_time)
        msg += "batch size: {}, ".format(self.config["data"]["batch_size"])
        msg += ", ".join("{}: {:>.6f}".format(k, v)
                         for k, v in losses_np.items())
        self.logger.info(msg)
        # print(msg)

        if dist.get_rank() == 0 and self.visualizer:
            for k, v in losses_np.items():
                self.visualizer.add_scalar("train/{}".format(k), v,
                                           self.iteration)
        self.iteration += 1

    @paddle.no_grad()
    def valid(self):
        self.logger.info(
            f"Valid Total Examples: {len(self.valid_loader.dataset)}")
        self.model.eval()
        valid_losses = defaultdict(list)
        num_seen_utts = 1
        total_loss = 0.0
        valid_total_label = []
        valid_total_predict = []
        for i, batch in enumerate(self.valid_loader):
            input, label = batch
            label = paddle.reshape(label, shape=[-1])
            y, logit = self.model(input)
            pred = paddle.argmax(logit, axis=1)
            valid_total_label.extend(label.numpy().tolist())
            valid_total_predict.extend(pred.numpy().tolist())
            loss = self.crit(y, label)

            if paddle.isfinite(loss):
                num_utts = batch[1].shape[0]
                num_seen_utts += num_utts
                total_loss += float(loss) * num_utts
                valid_losses["val_loss"].append(float(loss))

            if (i + 1) % self.config["training"]["log_interval"] == 0:
                valid_dump = {k: np.mean(v) for k, v in valid_losses.items()}
                valid_dump["val_history_loss"] = total_loss / num_seen_utts

                # logging
                msg = f"Valid: Rank: {dist.get_rank()}, "
                msg += "epoch: {}, ".format(self.epoch)
                msg += "step: {}, ".format(self.iteration)
                msg += "batch : {}/{}, ".format(i + 1, len(self.valid_loader))
                msg += ", ".join("{}: {:>.6f}".format(k, v)
                                 for k, v in valid_dump.items())
                self.logger.info(msg)
                # print(msg)

        self.logger.info("Rank {} Val info val_loss {}".format(
            dist.get_rank(), total_loss / num_seen_utts))
        # print("Rank {} Val info val_loss {} acc: {}".format(
        #     dist.get_rank(), total_loss / num_seen_utts, acc))
        F1_score = f1_score(
            valid_total_label, valid_total_predict, average="macro")
        return total_loss / num_seen_utts, F1_score

    def setup_model(self):
        config = self.config

        model = DefinedClassifier[self.config["model_type"]](
            **self.config["model_params"])
        self.crit = DefinedLoss[self.config["loss_type"]](**self.config[
            "loss"]) if "loss_type" in self.config else DefinedLoss["ce"]()

        if self.parallel:
            model = paddle.DataParallel(model)

        self.logger.info(f"{model}")
        layer_tools.print_params(model, self.logger.info)

        lr_scheduler = paddle.optimizer.lr.ExponentialDecay(
            learning_rate=config["training"]["lr"],
            gamma=config["training"]["lr_decay"],
            verbose=True)
        optimizer = paddle.optimizer.Adam(
            learning_rate=lr_scheduler,
            parameters=model.parameters(),
            weight_decay=paddle.regularizer.L2Decay(
                config["training"]["weight_decay"]))

        self.model = model
        self.optimizer = optimizer
        self.lr_scheduler = lr_scheduler
        self.logger.info("Setup model/criterion/optimizer/lr_scheduler!")

    def setup_dataloader(self):
        print("setup_dataloader!!!")
        config = self.config["data"].copy()

        print(config["batch_size"])

        train_dataset = DefinedDataset[config["dataset_type"]](
            train_path=config["train_path"], **config["data_params"])
        dev_dataset = DefinedDataset[config["dataset_type"]](
            train_path=config["dev_path"], **config["data_params"])

        # train_dataset = config["dataset_type"](os.path.join(config["save_path"], "train"), 
        #             os.path.join(config["save_path"], config["vocab_file"]),
        #             os.path.join(config["save_path"], config["punc_file"]),
        #             config["seq_len"])

        # dev_dataset = PuncDataset(os.path.join(config["save_path"], "dev"), 
        #             os.path.join(config["save_path"], config["vocab_file"]),
        #             os.path.join(config["save_path"], config["punc_file"]),
        #             config["seq_len"])

        # if self.parallel:
        #     batch_sampler = SortagradDistributedBatchSampler(
        #         train_dataset,
        #         batch_size=config["batch_size"],
        #         num_replicas=None,
        #         rank=None,
        #         shuffle=True,
        #         drop_last=True,
        #         sortagrad=config["sortagrad"],
        #         shuffle_method=config["shuffle_method"])
        # else:
        #     batch_sampler = SortagradBatchSampler(
        #         train_dataset,
        #         shuffle=True,
        #         batch_size=config["batch_size"],
        #         drop_last=True,
        #         sortagrad=config["sortagrad"],
        #         shuffle_method=config["shuffle_method"])

        self.train_loader = DataLoader(
            train_dataset,
            num_workers=config["num_workers"],
            batch_size=config["batch_size"])
        self.valid_loader = DataLoader(
            dev_dataset,
            batch_size=config["batch_size"],
            shuffle=False,
            drop_last=False,
            num_workers=config["num_workers"])
        self.logger.info("Setup train/valid Dataloader!")


class Tester(Trainer):
    def __init__(self, config, args):
        super().__init__(config, args)

    @mp_tools.rank_zero_only
    @paddle.no_grad()
    def test(self):
        self.logger.info(
            f"Test Total Examples: {len(self.test_loader.dataset)}")
        self.punc_list = []
        for i in range(len(self.test_loader.dataset.id2punc)):
            self.punc_list.append(self.test_loader.dataset.id2punc[i])
        self.model.eval()
        test_total_label = []
        test_total_predict = []
        with open(self.args.result_file, 'w') as fout:
            for i, batch in enumerate(self.test_loader):
                input, label = batch
                label = paddle.reshape(label, shape=[-1])
                y, logit = self.model(input)
                pred = paddle.argmax(logit, axis=1)
                test_total_label.extend(label.numpy().tolist())
                test_total_predict.extend(pred.numpy().tolist())
                # print(type(logit))

            # logging
        msg = "Test: "
        msg += "epoch: {}, ".format(self.epoch)
        msg += "step: {}, ".format(self.iteration)
        self.logger.info(msg)
        # print(msg)
        t = classification_report(
            test_total_label, test_total_predict, target_names=self.punc_list)
        print(t)
        t2 = self.evaluation(test_total_label, test_total_predict)
        print(t2)

    def evaluation(self, y_pred, y_test):
        precision, recall, f1, _ = precision_recall_fscore_support(
            y_test, y_pred, average=None, labels=[1, 2, 3])
        overall = precision_recall_fscore_support(
            y_test, y_pred, average='macro', labels=[1, 2, 3])
        result = pd.DataFrame(
            np.array([precision, recall, f1]),
            columns=list(['O', 'COMMA', 'PERIOD', 'QUESTION'])[1:],
            index=['Precision', 'Recall', 'F1'])
        result['OVERALL'] = overall[:3]
        return result

    def run_test(self):
        self.resume_or_scratch()
        try:
            self.test()
        except KeyboardInterrupt:
            exit(-1)

    def setup(self):
        """Setup the experiment.
        """
        paddle.set_device(self.args.device)
        self.setup_logger()
        self.setup_output_dir()
        self.setup_checkpointer()

        self.setup_dataloader()
        self.setup_model()

        self.iteration = 0
        self.epoch = 0

    def setup_model(self):
        config = self.config
        model = DefinedClassifier[self.config["model_type"]](
            **self.config["model_params"])

        self.model = model
        self.logger.info("Setup model!")

    def setup_dataloader(self):
        config = self.config["data"].copy()

        test_dataset = DefinedDataset[config["dataset_type"]](
            train_path=config["test_path"], **config["data_params"])

        self.test_loader = DataLoader(
            test_dataset,
            batch_size=config["batch_size"],
            shuffle=False,
            drop_last=False)
        self.logger.info("Setup test Dataloader!")

    def setup_output_dir(self):
        """Create a directory used for output.
        """
        # output dir
        if self.args.output:
            output_dir = Path(self.args.output).expanduser()
            output_dir.mkdir(parents=True, exist_ok=True)
        else:
            output_dir = Path(
                self.args.checkpoint_path).expanduser().parent.parent
            output_dir.mkdir(parents=True, exist_ok=True)

        self.output_dir = output_dir

    def setup_logger(self):
        LOG_FORMAT = "%(asctime)s - %(pathname)s[line:%(lineno)d] - %(levelname)s: %(message)s"
        format_str = logging.Formatter(
            '%(asctime)s - %(pathname)s[line:%(lineno)d] - %(levelname)s: %(message)s'
        )
        logging.basicConfig(
            filename=self.config["testing"]["log_path"],
            level=logging.INFO,
            format=LOG_FORMAT)
        self.logger = logging.getLogger(__name__)
        # self.logger = logging.getLogger(self.config["training"]["log_path"].strip().split('/')[-1].split('.')[0])

        self.logger.setLevel(logging.INFO)  #设置日志级别
        sh = logging.StreamHandler()  #往屏幕上输出
        sh.setFormatter(format_str)  #设置屏幕上显示的格式
        self.logger.addHandler(sh)  #把对象加到logger里

        self.logger.info('info')
        print("setup test logger!!!")

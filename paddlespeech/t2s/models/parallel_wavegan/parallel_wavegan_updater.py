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
from typing import Dict

import paddle
from paddle import distributed as dist
from paddle.io import DataLoader
from paddle.nn import Layer
from paddle.optimizer import Optimizer
from paddle.optimizer.lr import LRScheduler
from timer import timer

from paddlespeech.t2s.training.extensions.evaluator import StandardEvaluator
from paddlespeech.t2s.training.reporter import report
from paddlespeech.t2s.training.updaters.standard_updater import StandardUpdater
from paddlespeech.t2s.training.updaters.standard_updater import UpdaterState
logging.basicConfig(
    format='%(asctime)s [%(levelname)s] [%(filename)s:%(lineno)d] %(message)s',
    datefmt='[%Y-%m-%d %H:%M:%S]')
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


class PWGUpdater(StandardUpdater):
    def __init__(self,
                 models: Dict[str, Layer],
                 optimizers: Dict[str, Optimizer],
                 criterions: Dict[str, Layer],
                 schedulers: Dict[str, LRScheduler],
                 dataloader: DataLoader,
                 discriminator_train_start_steps: int,
                 lambda_adv: float,
                 output_dir=None):
        self.models = models
        self.generator: Layer = models['generator']
        self.discriminator: Layer = models['discriminator']

        self.optimizers = optimizers
        self.optimizer_g: Optimizer = optimizers['generator']
        self.optimizer_d: Optimizer = optimizers['discriminator']

        self.criterions = criterions
        self.criterion_stft = criterions['stft']
        self.criterion_mse = criterions['mse']

        self.schedulers = schedulers
        self.scheduler_g = schedulers['generator']
        self.scheduler_d = schedulers['discriminator']

        self.dataloader = dataloader

        self.discriminator_train_start_steps = discriminator_train_start_steps
        self.lambda_adv = lambda_adv
        self.state = UpdaterState(iteration=0, epoch=0)

        self.train_iterator = iter(self.dataloader)

        log_file = output_dir / 'worker_{}.log'.format(dist.get_rank())
        self.filehandler = logging.FileHandler(str(log_file))
        logger.addHandler(self.filehandler)
        self.logger = logger
        self.msg = ""

    def update_core(self, batch):
        self.msg = "Rank: {}, ".format(dist.get_rank())
        losses_dict = {}

        # parse batch
        wav, mel = batch

        # Generator
        noise = paddle.randn(wav.shape)

        with timer() as t:
            wav_ = self.generator(noise, mel)
            # logging.debug(f"Generator takes {t.elapse}s.")

        # initialize
        gen_loss = 0.0

        ## Multi-resolution stft loss
        with timer() as t:
            sc_loss, mag_loss = self.criterion_stft(wav_, wav)
            # logging.debug(f"Multi-resolution STFT loss takes {t.elapse}s.")

        report("train/spectral_convergence_loss", float(sc_loss))
        report("train/log_stft_magnitude_loss", float(mag_loss))

        losses_dict["spectral_convergence_loss"] = float(sc_loss)
        losses_dict["log_stft_magnitude_loss"] = float(mag_loss)

        gen_loss += sc_loss + mag_loss

        ## Adversarial loss
        if self.state.iteration > self.discriminator_train_start_steps:
            with timer() as t:
                p_ = self.discriminator(wav_)
                adv_loss = self.criterion_mse(p_, paddle.ones_like(p_))
                # logging.debug(
                #     f"Discriminator and adversarial loss takes {t.elapse}s")
            report("train/adversarial_loss", float(adv_loss))
            losses_dict["adversarial_loss"] = float(adv_loss)
            gen_loss += self.lambda_adv * adv_loss

        report("train/generator_loss", float(gen_loss))
        losses_dict["generator_loss"] = float(gen_loss)

        with timer() as t:
            self.optimizer_g.clear_grad()
            gen_loss.backward()
            # logging.debug(f"Backward takes {t.elapse}s.")

        with timer() as t:
            self.optimizer_g.step()
            self.scheduler_g.step()
            # logging.debug(f"Update takes {t.elapse}s.")

        # Disctiminator
        if self.state.iteration > self.discriminator_train_start_steps:
            with paddle.no_grad():
                wav_ = self.generator(noise, mel)
            p = self.discriminator(wav)
            p_ = self.discriminator(wav_.detach())
            real_loss = self.criterion_mse(p, paddle.ones_like(p))
            fake_loss = self.criterion_mse(p_, paddle.zeros_like(p_))
            dis_loss = real_loss + fake_loss
            report("train/real_loss", float(real_loss))
            report("train/fake_loss", float(fake_loss))
            report("train/discriminator_loss", float(dis_loss))
            losses_dict["real_loss"] = float(real_loss)
            losses_dict["fake_loss"] = float(fake_loss)
            losses_dict["discriminator_loss"] = float(dis_loss)

            self.optimizer_d.clear_grad()
            dis_loss.backward()

            self.optimizer_d.step()
            self.scheduler_d.step()

        self.msg += ', '.join('{}: {:>.6f}'.format(k, v)
                              for k, v in losses_dict.items())


class PWGEvaluator(StandardEvaluator):
    def __init__(self,
                 models,
                 criterions,
                 dataloader,
                 lambda_adv,
                 output_dir=None):
        self.models = models
        self.generator = models['generator']
        self.discriminator = models['discriminator']

        self.criterions = criterions
        self.criterion_stft = criterions['stft']
        self.criterion_mse = criterions['mse']

        self.dataloader = dataloader
        self.lambda_adv = lambda_adv

        log_file = output_dir / 'worker_{}.log'.format(dist.get_rank())
        self.filehandler = logging.FileHandler(str(log_file))
        logger.addHandler(self.filehandler)
        self.logger = logger
        self.msg = ""

    def evaluate_core(self, batch):
        # logging.debug("Evaluate: ")
        self.msg = "Evaluate: "
        losses_dict = {}

        wav, mel = batch
        noise = paddle.randn(wav.shape)

        with timer() as t:
            wav_ = self.generator(noise, mel)
            # logging.debug(f"Generator takes {t.elapse}s")

        ## Adversarial loss
        with timer() as t:
            p_ = self.discriminator(wav_)
            adv_loss = self.criterion_mse(p_, paddle.ones_like(p_))
            # logging.debug(
            #     f"Discriminator and adversarial loss takes {t.elapse}s")
        report("eval/adversarial_loss", float(adv_loss))
        losses_dict["adversarial_loss"] = float(adv_loss)
        gen_loss = self.lambda_adv * adv_loss

        # stft loss
        with timer() as t:
            sc_loss, mag_loss = self.criterion_stft(wav_, wav)
            # logging.debug(f"Multi-resolution STFT loss takes {t.elapse}s")

        report("eval/spectral_convergence_loss", float(sc_loss))
        report("eval/log_stft_magnitude_loss", float(mag_loss))
        losses_dict["spectral_convergence_loss"] = float(sc_loss)
        losses_dict["log_stft_magnitude_loss"] = float(mag_loss)
        gen_loss += sc_loss + mag_loss

        report("eval/generator_loss", float(gen_loss))
        losses_dict["generator_loss"] = float(gen_loss)

        # Disctiminator
        p = self.discriminator(wav)
        real_loss = self.criterion_mse(p, paddle.ones_like(p))
        fake_loss = self.criterion_mse(p_, paddle.zeros_like(p_))
        dis_loss = real_loss + fake_loss
        report("eval/real_loss", float(real_loss))
        report("eval/fake_loss", float(fake_loss))
        report("eval/discriminator_loss", float(dis_loss))

        losses_dict["real_loss"] = float(real_loss)
        losses_dict["fake_loss"] = float(fake_loss)
        losses_dict["discriminator_loss"] = float(dis_loss)

        self.msg += ', '.join('{}: {:>.6f}'.format(k, v)
                              for k, v in losses_dict.items())
        self.logger.info(self.msg)

#!/bin/bash

config_path=$1
train_output_path=$2
ckpt_name=$3
ge2e_params_path=$4
ref_audio_dir=$5

FLAGS_allocator_strategy=naive_best_fit \
FLAGS_fraction_of_gpu_memory_to_use=0.01 \
python3 ${BIN_DIR}/voice_cloning.py \
  --fastspeech2-config=${config_path} \
  --fastspeech2-checkpoint=${train_output_path}/checkpoints/${ckpt_name} \
  --fastspeech2-stat=dump/train/speech_stats.npy \
  --pwg-config=pwg_aishell3_ckpt_0.5/default.yaml \
  --pwg-checkpoint=pwg_aishell3_ckpt_0.5/snapshot_iter_1000000.pdz \
  --pwg-stat=pwg_aishell3_ckpt_0.5/feats_stats.npy \
  --ge2e_params_path=${ge2e_params_path} \
  --text="凯莫瑞安联合体的经济崩溃迫在眉睫。" \
  --input-dir=${ref_audio_dir} \
  --output-dir=${train_output_path}/vc_syn \
  --phones-dict=dump/phone_id_map.txt

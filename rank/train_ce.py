# Copyright (c) 2020 PaddlePaddle Authors. All Rights Reserved.
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

from functools import partial
import argparse
import os
import random
import time
import distutils.util

import numpy as np
import paddle
import paddle.nn.functional as F
from paddlenlp.data import Stack, Tuple, Pad
from paddlenlp.datasets import load_dataset
from paddlenlp.transformers import  AutoTokenizer,AutoModel
from paddlenlp.transformers import PolyDecayWithWarmup
from paddle.distributed.fleet.utils.hybrid_parallel_util import fused_allreduce_gradients
from model import CrossEncoder
from utils import convert_example, read_train_set, create_dataloader

# yapf: disable
parser = argparse.ArgumentParser()
parser.add_argument("--save_dir", default='./checkpoint', type=str, help="The output directory where the model checkpoints will be written.")
parser.add_argument("--train_set", type=str, required=True, help="The full path of train_set_file.")
parser.add_argument("--max_seq_length", default=128, type=int, help="The maximum total input sequence length after tokenization. "
    "Sequences longer than this will be truncated, sequences shorter will be padded.")
parser.add_argument("--batch_size", default=32, type=int, help="Batch size per GPU/CPU for training.")
parser.add_argument("--learning_rate", default=5e-5, type=float, help="The initial learning rate for Adam.")
parser.add_argument("--weight_decay", default=0.0, type=float, help="Weight decay if we apply some.")
parser.add_argument("--epochs", default=3, type=int, help="Total number of training epochs to perform.")
parser.add_argument("--warmup_proportion", default=0.0, type=float, help="Linear warmup proption over the training process.")
parser.add_argument("--valid_steps", default=100, type=int, help="The interval steps to evaluate model performance.")
parser.add_argument("--save_steps", default=100, type=int, help="The interval steps to save checkppoints.")
parser.add_argument("--logging_steps", default=10, type=int, help="The interval steps to logging.")
parser.add_argument("--init_from_ckpt", type=str, default=None, help="The path of checkpoint to be loaded.")
parser.add_argument("--seed", type=int, default=1000, help="random seed for initialization")
parser.add_argument('--device', choices=['cpu', 'gpu', 'xpu', 'npu'], default="gpu", help="Select which device to train model, defaults to gpu.")
parser.add_argument("--use_amp", type=distutils.util.strtobool, default=False, help="Enable mixed precision training.")
parser.add_argument("--scale_loss", type=float, default=2**15, help="The value of scale_loss for fp16.")
args = parser.parse_args()
# yapf: enable


def set_seed(seed):
    """sets random seed"""
    random.seed(seed)
    np.random.seed(seed)
    paddle.seed(seed)

def do_train():
    paddle.set_device(args.device)
    rank = paddle.distributed.get_rank()
    if paddle.distributed.get_world_size() > 1:
        paddle.distributed.init_parallel_env()
    dev_count = paddle.distributed.get_world_size()
    set_seed(args.seed)

    train_ds = load_dataset(read_train_set, data_path=args.train_set, lazy=False)

    pretrained_model = AutoModel.from_pretrained(
        'ernie-1.0')
    tokenizer = AutoTokenizer.from_pretrained('ernie-1.0')

    model = CrossEncoder(pretrained_model,num_classes=2)

    trans_func = partial(
        convert_example,
        tokenizer=tokenizer,
        max_seq_length=args.max_seq_length,
        is_pair=True)

    batchify_fn = lambda samples, fn=Tuple(
        Pad(axis=0, pad_val=tokenizer.pad_token_id),  # input
        Pad(axis=0, pad_val=tokenizer.pad_token_type_id),  # segment
        Stack(dtype="int64")  # label
    ): [data for data in fn(samples)]

    train_data_loader = create_dataloader(
        train_ds,
        mode='test',
        batch_size=args.batch_size,
        batchify_fn=batchify_fn,
        trans_fn=trans_func)

    if args.init_from_ckpt and os.path.isfile(args.init_from_ckpt):
        state_dict = paddle.load(args.init_from_ckpt)
        model.set_dict(state_dict)
    model = paddle.DataParallel(model)

    num_training_examples = len(train_ds)
    # 4卡 gpu
    max_train_steps = args.epochs * num_training_examples // args.batch_size // dev_count
    
    warmup_steps = int(max_train_steps * args.warmup_proportion)

    print("Device count: %d" % dev_count)
    print("Num train examples: %d" % num_training_examples)
    print("Max train steps: %d" % max_train_steps)
    print("Num warmup steps: %d" % warmup_steps)
    
    lr_scheduler = PolyDecayWithWarmup(args.learning_rate,max_train_steps,warmup_steps,lr_end=0.0,power=1.0)

    # Generate parameter names needed to perform weight decay.
    # All bias and LayerNorm parameters are excluded.
    decay_params = [
        p.name for n, p in model.named_parameters()
        if not any(nd in n for nd in ["bias", "norm"])
    ]
    optimizer = paddle.optimizer.AdamW(
        learning_rate=lr_scheduler,
        parameters=model.parameters(),
        weight_decay=args.weight_decay,
        apply_decay_param_fun=lambda x: x in decay_params,
        grad_clip=paddle.nn.ClipGradByGlobalNorm(1.0))

    criterion = paddle.nn.loss.CrossEntropyLoss()
    metric = paddle.metric.Accuracy()
    if args.use_amp:
        scaler = paddle.amp.GradScaler(init_loss_scaling=args.scale_loss)
    global_step = 0
    tic_train = time.time()
    for epoch in range(1, args.epochs + 1):
        for step, batch in enumerate(train_data_loader, start=1):
            input_ids, token_type_ids, labels = batch
            logits = model(input_ids, token_type_ids)
            loss = criterion(logits, labels)
            probs = F.softmax(logits, axis=1)
            correct = metric.compute(probs, labels)
            metric.update(correct)
            acc = metric.accumulate()
            loss.backward()

            optimizer.step()
            lr_scheduler.step()
            optimizer.clear_grad()

            global_step += 1
            if global_step % args.logging_steps == 0 and rank == 0:
                print("learning_rate: %f" %(lr_scheduler.get_lr()))
                time_diff = time.time() - tic_train
                print(
                    "global step %d, epoch: %d, batch: %d, loss: %.5f, accuracy: %.5f, speed: %.2f step/s"
                    % (global_step, epoch, step, loss, acc,
                       args.logging_steps / time_diff))
                tic_train = time.time()

            if global_step % args.save_steps == 0 and rank == 0:
                save_dir = os.path.join(args.save_dir, "model_%d" % global_step)
                paddle.save(model.state_dict(),os.path.join(save_dir,'model_state.pdparams'))
                tokenizer.save_pretrained(save_dir)
                tic_train = time.time()

    # save final checkpoint
    save_dir = os.path.join(args.save_dir, "model_%d" % global_step)
    paddle.save(model.state_dict(),os.path.join(save_dir,'model_state.pdparams'))
    tokenizer.save_pretrained(save_dir)


if __name__ == "__main__":
    do_train()

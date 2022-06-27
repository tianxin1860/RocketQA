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

from functools import partial
import argparse
import os
import sys
import random
import time

import numpy as np
import paddle
import paddle.nn.functional as F

import paddlenlp as ppnlp
from paddlenlp.data import Stack, Tuple, Pad
from paddlenlp.datasets import load_dataset
from paddlenlp.transformers import LinearDecayWithWarmup
from paddle.distributed.fleet.utils.hybrid_parallel_util import fused_allreduce_gradients

from model import DualEncoder
from data import read_train_data, convert_train_example, create_dataloader

# yapf: disable
parser = argparse.ArgumentParser()
parser.add_argument("--save_dir", default='./checkpoint', type=str, help="The output directory where the model checkpoints will be written.")
parser.add_argument("--query_max_seq_length", default=32, type=int, help="The maximum total input sequence length of query after tokenization. "
    "Sequences longer than this will be truncated, sequences shorter will be padded.")
parser.add_argument("--title_max_seq_length", default=128, type=int, help="The maximum total input sequence length of title after tokenization. "
    "Sequences longer than this will be truncated, sequences shorter will be padded.")
parser.add_argument("--batch_size", default=32, type=int, help="Batch size per GPU/CPU for training.")
parser.add_argument("--output_emb_size", default=None, type=int, help="output_embedding_size")
parser.add_argument("--learning_rate", default=3e-5, type=float, help="The initial learning rate for Adam.")
parser.add_argument("--weight_decay", default=0.0, type=float, help="Weight decay if we apply some.")
parser.add_argument("--epochs", default=10, type=int, help="Total number of training epochs to perform.")
parser.add_argument("--warmup_proportion", default=0.0, type=float, help="Linear warmup proption over the training process.")
parser.add_argument("--init_from_ckpt", type=str, default=None, help="The path of checkpoint to be loaded.")
parser.add_argument("--seed", type=int, default=1000, help="random seed for initialization")
parser.add_argument('--device', choices=['cpu', 'gpu'], default="gpu", help="Select which device to train model, defaults to gpu.")
parser.add_argument('--save_steps', type=int, default=10000, help="Inteval steps to save checkpoint")
parser.add_argument("--train_set_file", type=str, required=True, help="The full path of train_set_file")
parser.add_argument("--use_cross_batch",  action="store_true", help="Whether to use cross-batch for training.")
parser.add_argument(
        '--use_amp',
        action='store_true',
        help='Whether to use float16(Automatic Mixed Precision) to train.')
parser.add_argument("--scale_loss", type=float, default=102400, help="The value of scale_loss for fp16. This is only used for AMP training.")


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

    set_seed(args.seed)
    
    dev_count = paddle.distributed.get_world_size()

    train_ds = load_dataset(
        read_train_data, data_path=args.train_set_file, lazy=False)

    # If you wanna use bert/roberta pretrained model,
    query_model = ppnlp.transformers.ErnieModel.from_pretrained(
        'ernie-1.0')
    title_model = ppnlp.transformers.ErnieModel.from_pretrained(
        'ernie-1.0')

    # If you wanna use bert/roberta pretrained model,
    tokenizer = ppnlp.transformers.ErnieTokenizer.from_pretrained(
        'ernie-1.0')

    trans_func = partial(
        convert_train_example,
        tokenizer=tokenizer,
        query_max_seq_length=args.query_max_seq_length,
        title_max_seq_length=args.title_max_seq_length)

    batchify_fn = lambda samples, fn=Tuple(
        Pad(axis=0, pad_val=tokenizer.pad_token_id),  # query_input
        Pad(axis=0, pad_val=tokenizer.pad_token_type_id),  # query_segment
        Pad(axis=0, pad_val=tokenizer.pad_token_id),  # pos_title_input
        Pad(axis=0, pad_val=tokenizer.pad_token_type_id),  # pos_tilte_segment
        Pad(axis=0, pad_val=tokenizer.pad_token_id),  # pos_title_input
        Pad(axis=0, pad_val=tokenizer.pad_token_type_id),  # pos_tilte_segment
    ): [data for data in fn(samples)]

    train_data_loader = create_dataloader(
        train_ds,
        mode='train',
        batch_size=args.batch_size,
        batchify_fn=batchify_fn,
        trans_fn=trans_func)

    model = DualEncoder(query_model,title_model, use_cross_batch=args.use_cross_batch)

    if args.init_from_ckpt and os.path.isfile(args.init_from_ckpt):
        state_dict = paddle.load(args.init_from_ckpt)
        model.set_dict(state_dict)
        print("warmup from:{}".format(args.init_from_ckpt))

    model = paddle.DataParallel(model)

    num_training_examples = len(train_ds)
    # 4卡 gpu
    max_train_steps = args.epochs * num_training_examples // args.batch_size // dev_count
    
    warmup_steps = int(max_train_steps * args.warmup_proportion)

    print("Device count: %d" % dev_count)
    print("Num train examples: %d" % num_training_examples)
    print("Max train steps: %d" % max_train_steps)
    print("Num warmup steps: %d" % warmup_steps)

    
    # lr_scheduler = LinearDecayWithWarmup(args.learning_rate, num_training_steps,
    #                                      args.warmup_proportion)
    lr_scheduler = LinearDecayWithWarmup(args.learning_rate, max_train_steps,
                                         warmup_steps)

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
        apply_decay_param_fun=lambda x: x in decay_params)

    if args.use_amp:
        scaler = paddle.amp.GradScaler(init_loss_scaling=args.scale_loss,
                                                    decr_ratio=0.8,
                                                    incr_ratio=2.0,
                                                    incr_every_n_steps=100,
                                                    decr_every_n_nan_or_inf=2,
                                                    use_dynamic_loss_scaling=True)

    global_step = 0
    tic_train = time.time()

    avg_loss = 0.0
    for epoch in range(1, args.epochs + 1):
        for step, batch in enumerate(train_data_loader, start=1):
            (query_input_ids, query_token_type_ids, pos_title_input_ids,
             pos_title_token_type_ids, neg_title_input_ids,
             neg_title_token_type_ids) = batch
            if args.use_amp:
                with model.no_sync():
                    with paddle.amp.auto_cast(args.use_amp,
                    custom_white_list=['layer_norm', 'softmax', 'gelu', 'tanh']):
                        loss, accuracy = model(
                            query_input_ids=query_input_ids,
                            pos_title_input_ids=pos_title_input_ids,
                            neg_title_input_ids=neg_title_input_ids,
                            query_token_type_ids=query_token_type_ids,
                            pos_title_token_type_ids=pos_title_token_type_ids,
                            neg_title_token_type_ids=neg_title_token_type_ids)
                    scaler.scale(loss).backward()
                    # step 2 : fuse + allreduce manually before optimization
                    fused_allreduce_gradients(list(model.parameters()), None)
                    scaler.minimize(optimizer, loss)

                    avg_loss += loss

                    global_step += 1
                    if global_step % 10 == 0:
                        print("learning_rate: %f" %(lr_scheduler.get_lr()))
                        print(
                            "global step %d, epoch: %d, batch: %d, loss: %.2f, avg_loss: %.2f, accuracy:%.2f, speed: %.2f step/s"
                            % (global_step, epoch, step, loss,
                            avg_loss / global_step, 100 * accuracy,
                            10 / (time.time() - tic_train)))
                        tic_train = time.time()
            else:
                # skip gradient synchronization by 'no_sync'
                with model.no_sync():
                    loss, accuracy = model(
                        query_input_ids=query_input_ids,
                        pos_title_input_ids=pos_title_input_ids,
                        neg_title_input_ids=neg_title_input_ids,
                        query_token_type_ids=query_token_type_ids,
                        pos_title_token_type_ids=pos_title_token_type_ids,
                        neg_title_token_type_ids=neg_title_token_type_ids)
                    avg_loss += loss

                    global_step += 1
                    if global_step % 10 == 0:
                        print("learning_rate: %f" %(lr_scheduler.get_lr()))
                        print(
                            "global step %d, epoch: %d, batch: %d, loss: %.2f, avg_loss: %.2f, accuracy:%.2f, speed: %.2f step/s"
                            % (global_step, epoch, step, loss,
                            avg_loss / global_step, 100 * accuracy,
                            10 / (time.time() - tic_train)))
                        tic_train = time.time()
                    loss.backward()
                # step 2 : fuse + allreduce manually before optimization
                fused_allreduce_gradients(list(model.parameters()), None)
                optimizer.step()
            
            lr_scheduler.step()
            optimizer.clear_grad()
            if global_step % args.save_steps == 0 and rank == 0:
                save_dir = os.path.join(args.save_dir, "model_%d" % global_step)
                if not os.path.exists(save_dir):
                    os.makedirs(save_dir)
                save_param_path = os.path.join(save_dir, 'query_model_state.pdparams')
                paddle.save(query_model.state_dict(), save_param_path)
                save_param_path = os.path.join(save_dir, 'title_model_state.pdparams')
                paddle.save(title_model.state_dict(), save_param_path)
                tokenizer.save_pretrained(save_dir)

    save_dir = os.path.join(args.save_dir, "model_%d" % global_step)
    save_param_path = os.path.join(save_dir, 'query_model_state.pdparams')
    paddle.save(query_model.state_dict(), save_param_path)
    save_param_path = os.path.join(save_dir, 'title_model_state.pdparams')
    paddle.save(title_model.state_dict(), save_param_path)
    tokenizer.save_pretrained(save_dir)


if __name__ == "__main__":
    do_train()

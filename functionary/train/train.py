import json
import math
import pathlib
import random
from dataclasses import dataclass, field
from typing import Dict, Optional

import torch
import torch.distributed
from torch.nn import CrossEntropyLoss

from functionary.prompt import EndToken
from functionary.train.datasets import CustomDataset
from functionary.train.llama_flash_attn_monkey_patch import (
    replace_llama_attn_with_flash_attn,
)

replace_llama_attn_with_flash_attn()

import transformers
from transformers import LlamaTokenizer, Trainer


@dataclass
class ModelArguments:
    model_name_or_path: Optional[str] = field(default="meta-llama/Llama-2-7b-hf")


@dataclass
class DataArguments:
    train_data_path: str = field(
        default=None, metadata={"help": "Path to the training data."}
    )
    eval_data_path: str = field(
        default=None, metadata={"help": "Path to the eval data."}
    )


@dataclass
class TrainingArguments(transformers.TrainingArguments):
    cache_dir: Optional[str] = field(default=None)
    optim: str = field(default="adamw_torch")
    model_max_length: int = field(
        default=4096,
        metadata={
            "help": "Maximum sequence length. Sequences will be right padded (and possibly truncated)."
        },
    )


def safe_save_model_for_hf_trainer(trainer: transformers.Trainer, output_dir: str):
    """Collects the state dict and dump to disk."""
    state_dict = trainer.model.state_dict()
    if trainer.args.should_save:
        cpu_state_dict = {key: value.cpu() for key, value in state_dict.items()}
        del state_dict
        trainer._save(output_dir, state_dict=cpu_state_dict)  # noqa


def initialize_tokenizer(
    model: transformers.AutoModelForCausalLM,
    model_name_or_path: str,
    model_max_length: int,
    cache_dir: str,
):
    """Initialize tokenizer and add special tokens, resizing vocab and embedding"""
    # note that must set legacy=True, read more: https://github.com/huggingface/transformers/issues/25176
    tokenizer = LlamaTokenizer.from_pretrained(
        model_name_or_path,
        cache_dir=cache_dir,
        model_max_length=model_max_length,
        padding_side="right",
        legacy=True,
    )

    # Add special tokens
    tokenizer.pad_token = tokenizer.unk_token
    special_tokens = {"additional_special_tokens": [e.value for e in EndToken]}
    num_new_tokens = tokenizer.add_special_tokens(special_tokens)

    # Resize embedding
    model.resize_token_embeddings(len(tokenizer))
    if num_new_tokens > 0:
        input_embeddings = model.get_input_embeddings().weight.data
        output_embeddings = model.get_output_embeddings().weight.data

        input_embeddings_avg = input_embeddings[:-num_new_tokens].mean(
            dim=0, keepdim=True
        )
        output_embeddings_avg = output_embeddings[:-num_new_tokens].mean(
            dim=0, keepdim=True
        )

        input_embeddings[-num_new_tokens:] = input_embeddings_avg
        output_embeddings[-num_new_tokens:] = output_embeddings_avg

    return tokenizer


def train():
    argument_parser = transformers.HfArgumentParser(
        (ModelArguments, DataArguments, TrainingArguments)
    )
    model_args, data_args, training_args = argument_parser.parse_args_into_dataclasses()

    # Set RoPE scaling factor
    config = transformers.AutoConfig.from_pretrained(
        model_args.model_name_or_path,
        cache_dir=training_args.cache_dir,
    )
    orig_ctx_len = getattr(config, "max_position_embeddings", None)
    if orig_ctx_len and training_args.model_max_length > orig_ctx_len:
        scaling_factor = float(math.ceil(training_args.model_max_length / orig_ctx_len))
        config.rope_scaling = {"type": "linear", "factor": scaling_factor}
    config.use_cache = False

    model = transformers.AutoModelForCausalLM.from_pretrained(
        model_args.model_name_or_path,
        config=config,
        cache_dir=training_args.cache_dir,
    )
    model.config.use_cache = False

    tokenizer = initialize_tokenizer(
        model,
        model_args.model_name_or_path,
        training_args.model_max_length,
        training_args.cache_dir,
    )

    assert data_args.train_data_path is not None, "Please provide a training data file."

    with open(data_args.train_data_path, "r") as file:
        raw_train_data = [json.loads(line) for line in file]
    random.shuffle(raw_train_data)

    if data_args.eval_data_path is not None:
        with open(data_args.eval_data_path, "r") as file:
            raw_eval_data = [json.loads(line) for line in file]
        random.shuffle(raw_eval_data)

    train_dataset = CustomDataset(raw_train_data, tokenizer)

    if torch.distributed.get_rank() == 0:
        print(f"Training Data Loaded: #{len(raw_train_data)}")

    if training_args.do_eval:
        eval_dataset = CustomDataset(raw_eval_data, tokenizer)

        if torch.distributed.get_rank() == 0:
            print(f"Eval Data Loaded: #{len(raw_eval_data)}")

    def preprocess_logits_for_metrics(logits, labels):
        """Preprocesses the logits during evaluation by computing the greedy token predictions for
        accuracy calculation and loss values for perplexity calculation. Both pred_ids and loss are
        of shape (batch_size x seq_len)"""
        pred_ids = torch.argmax(logits, dim=-1)

        loss_fn = CrossEntropyLoss(reduction="none")
        shift_logits = logits[..., :-1, :].contiguous()
        shift_labels = labels[..., 1:].contiguous()
        shift_logits = shift_logits.view(-1, len(tokenizer))
        shift_labels = shift_labels.view(-1)
        loss = loss_fn(shift_logits, shift_labels)
        loss = torch.mean(loss.view(logits.shape[0], -1), dim=-1)

        return pred_ids, loss

    def compute_metrics(eval_preds):
        """Computes next-token accuracy and perplexity metrics for evaluation"""
        predictions = eval_preds.predictions[0][:, :-1]
        labels = eval_preds.label_ids[:, 1:]

        # Calculate accuracy
        acc_count = 0
        total_num = 0
        for pred, label in zip(
            predictions.flatten().tolist(), labels.flatten().tolist()
        ):
            if label != -100:
                if label == pred:
                    acc_count += 1
                total_num += 1

        # Calculate perplexity
        loss = eval_preds.predictions[1].tolist()
        loss = sum(loss) / len(loss)
        perplexity = math.exp(loss)

        return {"accuracy": acc_count / total_num, "perplexity": perplexity}

    if training_args.do_eval:
        trainer = Trainer(
            model=model,
            tokenizer=tokenizer,
            args=training_args,
            train_dataset=train_dataset,
            eval_dataset=eval_dataset,
            compute_metrics=compute_metrics,
            preprocess_logits_for_metrics=preprocess_logits_for_metrics,
        )
    else:
        trainer = Trainer(
            model=model,
            tokenizer=tokenizer,
            args=training_args,
            train_dataset=train_dataset,
        )

    if list(pathlib.Path(training_args.output_dir).glob("checkpoint-*")):
        trainer.train(resume_from_checkpoint=True)
    else:
        trainer.train()
    trainer.save_state()
    safe_save_model_for_hf_trainer(trainer=trainer, output_dir=training_args.output_dir)


if __name__ == "__main__":
    train()

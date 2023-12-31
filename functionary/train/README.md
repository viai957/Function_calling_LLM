```shell
# Create new virtual environment
python3 -m venv venv && source venv/bin/activate

# Install Torch 2.0.1
pip3 install torch==2.0.1 torchvision==0.15.2 torchaudio==2.0.2 --index-url https://download.pytorch.org/whl/cu118

# Install Dependencies
pip install accelerate==0.23.0 transformers==4.33.3 sentencepiece==0.1.99 packaging==23.1 ninja==1.11.1 einops==0.7.0 wandb==0.15.11 jsonref==1.1.0 pydantic==2.1.1

# Install Flash Attention 2
pip install flash-attn==2.3.0 --no-build-isolation

# 2xA100 80GB, from the root directory of the repository
accelerate launch --config_file "functionary/train/fsdp_config.yaml" -m functionary.train.train \
    --model_name_or_path meta-llama/Llama-2-13b-hf  \
    --train_data_path train_dataset.jsonl \
    --eval_data_path eval_dataset.jsonl \
    --bf16 True \
    --num_train_epochs 2 \
    --per_device_train_batch_size 1 \
    --gradient_accumulation_steps 32 \
    --per_device_eval_batch_size 1 \
    --eval_accumulation_steps 1 \
    --evaluation_strategy "steps" \
    --eval_steps 400 \
    --save_strategy "steps" \
    --save_steps 200 \
    --save_total_limit 5 \
    --learning_rate 2e-5 \
    --weight_decay 0.3 \
    --warmup_ratio 0.03 \
    --lr_scheduler_type "cosine" \
    --logging_steps 1 \
    --model_max_length 4096 \
    --optim "paged_adamw_32bit" \
    --gradient_checkpointing True \
    --output_dir functionary-v1


# Without accelerate:
# 2xA100 80GB, from the root directory of the repository
torchrun --nproc_per_node=2 --master_port=20001 -m functionary.train.train \
    --model_name_or_path meta-llama/Llama-2-7b-hf  \
    --train_data_path llama_train_dataset.jsonl \
    --eval_data_path llama_eval_dataset.jsonl \
    --bf16 True \
    --num_train_epochs 2 \
    --per_device_train_batch_size 2 \
    --gradient_accumulation_steps 16 \
    --per_device_eval_batch_size 4 \
    --eval_accumulation_steps 16 \
    --evaluation_strategy "steps" \
    --eval_steps 400 \
    --save_strategy "steps" \
    --save_steps 200 \
    --save_total_limit 5 \
    --learning_rate 2e-5 \
    --weight_decay 0.3 \
    --warmup_ratio 0.03 \
    --lr_scheduler_type "cosine" \
    --logging_steps 1 \
    --fsdp "full_shard auto_wrap" \
    --fsdp_transformer_layer_cls_to_wrap 'LlamaDecoderLayer' \
    --tf32 True \
    --model_max_length 4096 \
    --report_to all \
    --gradient_checkpointing True \
    --output_dir functionary-v1
```

*If the training is stuck before training, run the following command before starting the training*:

```shell
export NCCL_P2P_DISABLE=1
```
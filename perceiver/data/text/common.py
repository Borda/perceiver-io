import hashlib
import os
from enum import Enum
from itertools import chain
from typing import Optional, Sequence

import pytorch_lightning as pl
import torch
from datasets import Dataset, DatasetDict
from torch.utils.data import DataLoader
from transformers import AutoTokenizer

from perceiver.data.text.collator import (
    DefaultCollator,
    RandomTruncateCollator,
    TokenMaskingCollator,
    WordMaskingCollator,
)
from perceiver.data.text.utils import PerceiverTokenizerUtil


os.environ["TOKENIZERS_PARALLELISM"] = "false"


class TextPreprocessor:
    def __init__(self, tokenizer: str, max_seq_len: int, add_special_tokens: bool):
        self.tokenizer = AutoTokenizer.from_pretrained(tokenizer, verbose=False)
        self.max_seq_len = max_seq_len
        self.add_special_tokens = add_special_tokens

    def preprocess(self, text):
        xs, pad_mask = self.preprocess_batch([text])
        return xs[0], pad_mask[0]

    def preprocess_batch(self, text_batch):
        result = self.tokenizer(
            text_batch,
            padding=self.tokenizer.pad_token is not None,
            truncation=True,
            add_special_tokens=self.add_special_tokens,
            return_token_type_ids=False,
            return_attention_mask=True,
            max_length=self.max_seq_len,
            return_tensors="pt",
        )
        return result["input_ids"], ~result["attention_mask"].type(torch.bool)


class Task(Enum):
    mlm = 0
    clm = 1
    clf = 2


class TextDataModule(pl.LightningDataModule):
    def __init__(
        self,
        dataset_dir: str,
        tokenizer: str,
        max_seq_len: int,
        task: Task = Task.mlm,
        mask_prob: float = 0.15,
        mask_words: bool = True,
        static_masking: bool = False,
        add_special_tokens: bool = False,
        add_eos_token: bool = False,
        padding_side: Optional[str] = None,
        random_train_shift: bool = False,
        random_valid_shift: bool = False,
        random_train_truncation: bool = False,
        random_valid_truncation: bool = False,
        random_min_seq_len: int = 16,
        preproc_batch_size: int = 1000,
        preproc_workers: Optional[int] = None,
        batch_size: int = 64,
        valid_batch_size: Optional[int] = None,
        num_workers: int = 3,
        pin_memory: bool = True,
    ):
        """Base class for consistent data preprocessing and loading across different text data sources.

        :param dataset_dir: Directory for storing the preprocessed dataset.
        :param tokenizer: Reference to a Hugging Face fast tokenizer (or the `deepmind/language-perceiver` tokenizer).
        :param max_seq_len: Maximum sequence length generated by this data module.
        :param task: The task for which this data module is used. Data are preprocessed and loaded in a task-specific
            way.
        :param mask_prob: Masking probability. Ignored if task is not `Task.mlm`.
        :param mask_words: Whether to mask words or individual tokens. Ignored if task is not `Task.mlm`.
        :param static_masking: Whether to mask at preprocessing time (static) or at data loading time (dynamic).
            Ignored if task is not `Task.mlm`.
        :param add_special_tokens: Whether to add special tokens to tokenized text.
        :param add_eos_token: Whether to append an EOS tokens to each example.
        :param padding_side: If `None`, uses the pre-configured `padding_side` of the tokenizer. Can be overridden by
            setting to "left" or "right".
        :param random_train_truncation: Randomly truncates sequences in the training set to length
            `randint(random_min_seq_len, max_seq_len + 1)`.
        :param random_valid_truncation: Randomly truncates sequences in the validation set to length
            `randint(random_min_seq_len, max_seq_len + 1)`.
        :param random_min_seq_len: Minimum sequence length when using `random_train_truncation` or
            `random_valid_truncation`.
        :param preproc_batch_size: Preprocessing batch size.
        :param preproc_workers: Number of preprocessing processes. If not defined, defaults to `num_workers`.
        :param batch_size: Batch size of loaded training data.
        :param valid_batch_size: Batch size of loaded validation data. If `None` defaults to `batch_size`
        :param num_workers: Number of data loading processes.
        """

        super().__init__()
        self.save_hyperparameters()

        if self.hparams.static_masking and not self.hparams.mask_words:
            raise ValueError("static_masking=true is only supported for mask_words=true")

        self.tokenizer = AutoTokenizer.from_pretrained(self.hparams.tokenizer, verbose=False)

        if self.hparams.padding_side is not None:
            self.tokenizer.padding_side = self.hparams.padding_side

        # PerceiverTokenizer needs special support for generating word_ids as it is not a fast tokenizer
        self.perceiver_tokenizer_configured = self.hparams.tokenizer in [
            "krasserm/perceiver-io-mlm",
            "deepmind/language-perceiver",
        ]
        if self.perceiver_tokenizer_configured:
            self.perceiver_tokenizer_util = PerceiverTokenizerUtil(self.tokenizer)

        if self.hparams.task == Task.mlm and not self.hparams.static_masking:
            if self.hparams.mask_words:
                self.collator = WordMaskingCollator(tokenizer=self.tokenizer, mask_prob=self.hparams.mask_prob)
            else:
                self.collator = TokenMaskingCollator(tokenizer=self.tokenizer, mask_prob=self.hparams.mask_prob)
        else:
            self.collator = DefaultCollator(tokenizer=self.tokenizer, max_seq_len=self.hparams.max_seq_len)

        self.ds_train = None
        self.ds_valid = None

    @property
    def valid_batch_size(self):
        if self.hparams.valid_batch_size is None:
            return self.hparams.batch_size
        else:
            return self.hparams.valid_batch_size

    @property
    def vocab_size(self):
        return self.tokenizer.vocab_size

    @property
    def max_seq_len(self):
        return self.hparams.max_seq_len

    @property
    def random_shift(self):
        return self.hparams.random_train_shift or self.hparams.random_valid_shift

    @property
    def preproc_workers(self):
        if self.hparams.preproc_workers is not None:
            return self.hparams.preproc_workers
        else:
            return max(1, self.hparams.num_workers)

    @property
    def preproc_dir(self):
        h = hashlib.new("md5")
        h.update(self.preproc_dir_hash_input().encode())
        return os.path.join(self.hparams.dataset_dir, "preproc", h.hexdigest())

    def preproc_dir_hash_input(self) -> str:
        hash_input = f"{self.hparams.tokenizer}-{self.max_seq_len}-{self.hparams.task.name}-{self.random_shift}"
        if self.hparams.task == Task.mlm and self.hparams.static_masking:
            hash_input = f"{hash_input}-{self.hparams.mask_words}-{self.hparams.mask_prob}"
        if self.hparams.add_special_tokens:
            hash_input = f"{hash_input}-st"
        if self.hparams.add_eos_token:
            hash_input = f"{hash_input}-eos"
        if self.hparams.get("source_train_size") is not None:
            hash_input = f"{hash_input}-ts-{self.hparams.source_train_size}"
        if self.hparams.get("source_valid_size") is not None:
            hash_input = f"{hash_input}-vs-{self.hparams.source_valid_size}"
        return hash_input

    def prepare_data(self) -> None:
        if not os.path.exists(self.preproc_dir):
            dataset = self.load_source_dataset()
            dataset = self._prepare_dataset(dataset)
            dataset.save_to_disk(self.preproc_dir)

    def setup(self, stage=None):
        dataset = self.load_prepared_dataset()

        self.ds_train = dataset["train"]
        self.ds_valid = dataset["valid"]

        if self.hparams.task in [Task.clm, Task.mlm]:
            if self.hparams.random_train_shift:
                self.ds_train = RandomShiftDataset(self.ds_train)
            if self.hparams.random_valid_shift:
                self.ds_valid = RandomShiftDataset(self.ds_valid)

        if self.hparams.task == Task.clm:
            self.ds_train = CLMDataset(self.ds_train)
            self.ds_valid = CLMDataset(self.ds_valid)

    def train_dataloader(self):
        if self.hparams.random_train_truncation:
            collator = RandomTruncateCollator(self.collator, self.hparams.random_min_seq_len)
        else:
            collator = self.collator

        return DataLoader(
            self.ds_train,
            shuffle=True,
            collate_fn=collator,
            batch_size=self.hparams.batch_size,
            num_workers=self.hparams.num_workers,
            pin_memory=self.hparams.pin_memory,
        )

    def val_dataloader(self):
        if self.hparams.random_valid_truncation:
            collator = RandomTruncateCollator(self.collator, self.hparams.random_min_seq_len)
        else:
            collator = self.collator

        return DataLoader(
            self.ds_valid,
            shuffle=False,
            collate_fn=collator,
            batch_size=self.valid_batch_size,
            num_workers=self.hparams.num_workers,
            pin_memory=self.hparams.pin_memory,
        )

    def text_preprocessor(self):
        preproc = TextPreprocessor(
            tokenizer=self.hparams.tokenizer,
            max_seq_len=self.hparams.max_seq_len,
            add_special_tokens=self.hparams.add_special_tokens,
        )

        if self.hparams.padding_side is not None:
            preproc.tokenizer.padding_side = self.hparams.padding_side

        return preproc

    def load_source_dataset(self) -> DatasetDict:
        """Must return a DatasetDict with keys 'train' and 'valid'."""
        raise NotImplementedError()

    def load_prepared_dataset(self) -> DatasetDict:
        return DatasetDict.load_from_disk(self.preproc_dir)

    def _prepare_dataset(self, dataset: DatasetDict):
        if self.hparams.task == Task.clm:
            dataset = self._tokenize_dataset(dataset, return_word_ids=False)
            dataset = self._chunk_dataset(dataset, chunk_size=self.hparams.max_seq_len + 1, include_keys=["input_ids"])
        elif self.hparams.task == Task.mlm:
            dataset = self._tokenize_dataset(dataset, return_word_ids=True)
            dataset = self._chunk_dataset(dataset, chunk_size=self.hparams.max_seq_len)
            if self.hparams.static_masking:
                dataset = self._mask_dataset(dataset)
        else:  # task == Task.clf
            assert "label" in dataset["train"].column_names
            assert "label" in dataset["valid"].column_names
            dataset = self._tokenize_dataset(
                dataset, max_length=self.max_seq_len, truncation=True, return_word_ids=False
            )
        return dataset

    def _tokenize_dataset(
        self,
        dataset: DatasetDict,
        padding=False,
        truncation=False,
        max_length=None,
        return_word_ids=True,
    ):
        def tokenize(examples):
            if self.hparams.add_eos_token:
                examples["text"] = [text + self.tokenizer.eos_token for text in examples["text"]]
            encoding = self.tokenizer(
                examples["text"],
                padding=padding,
                truncation=truncation,
                max_length=max_length,
                add_special_tokens=self.hparams.add_special_tokens,
                return_token_type_ids=False,
                return_attention_mask=False,
            )
            if return_word_ids:
                if self.perceiver_tokenizer_configured:
                    encoding["word_ids"] = [
                        self.perceiver_tokenizer_util.word_ids(input_ids) for input_ids in encoding["input_ids"]
                    ]
                else:
                    encoding["word_ids"] = [encoding.word_ids(i) for i in range(len(encoding["input_ids"]))]
            return encoding

        result = DatasetDict()
        for key in dataset.keys():
            result[key] = dataset[key].map(
                tokenize,
                batched=True,
                batch_size=self.hparams.preproc_batch_size,
                num_proc=self.preproc_workers,
                remove_columns=["text"],
                load_from_cache_file=False,
                desc="Running tokenizer on dataset",
            )
        return result

    def _chunk_dataset(
        self,
        dataset: DatasetDict,
        chunk_size: int,
        include_keys: Sequence[str] = ("input_ids", "word_ids"),
        remove_keys: Sequence[str] = (),
    ):
        def chunk(*args):
            chained = {k: list(chain(*args[i])) for i, k in enumerate(include_keys)}
            chained_len = len(chained[include_keys[0]])
            if chained_len >= chunk_size:
                chained_len = (chained_len // chunk_size) * chunk_size
            return {k: [t[i : i + chunk_size] for i in range(0, chained_len, chunk_size)] for k, t in chained.items()}

        result = DatasetDict()
        for key in dataset.keys():
            result[key] = dataset[key].map(
                chunk,
                batched=True,
                batch_size=self.hparams.preproc_batch_size,
                num_proc=self.preproc_workers,
                input_columns=list(include_keys),
                remove_columns=list(remove_keys),
                load_from_cache_file=False,
                desc=f"Split dataset into chunks of size {chunk_size}",
            )
        return result

    def _mask_dataset(self, dataset: DatasetDict):
        wmc = WordMaskingCollator(tokenizer=self.tokenizer, mask_prob=self.hparams.mask_prob)

        def mask(example):
            return wmc.mask_words_1(example)

        result = DatasetDict()
        for key in dataset.keys():
            result[key] = dataset[key].map(
                mask,
                batched=False,
                num_proc=self.preproc_workers,
                load_from_cache_file=False,
                desc="Mask words in dataset",
            )
        return result

    def _train_valid_split(self, dataset: Dataset, train_size, test_size):
        dataset = dataset.train_test_split(train_size=train_size, test_size=test_size, shuffle=not self.random_shift)
        return DatasetDict(train=dataset["train"], valid=dataset["test"])


class RandomShiftDataset(torch.utils.data.Dataset):
    def __init__(self, dataset):
        self.dataset = dataset

    def __getitem__(self, idx):
        example_1 = self.dataset[idx]
        example_2 = self.dataset[idx + 1]

        result = {}
        shift = None

        for key in example_1.keys():
            record_1 = example_1[key]
            record_2 = example_2[key]

            if shift is None:
                shift = torch.randint(len(record_1), (1,)).item()

            result[key] = record_1[shift:] + record_2[:shift]

        return result

    def __len__(self):
        return len(self.dataset) - 1


class CLMDataset(torch.utils.data.Dataset):
    def __init__(self, dataset):
        self.dataset = dataset

    def __getitem__(self, idx):
        record = self.dataset[idx]["input_ids"]
        return {"input_ids": record[:-1], "label_ids": record[1:]}

    def __len__(self):
        return len(self.dataset)

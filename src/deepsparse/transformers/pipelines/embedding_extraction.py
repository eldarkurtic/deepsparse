# Copyright (c) 2021 - present / Neuralmagic, Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# postprocessing adapted from huggingface/transformers

# Copyright 2021 The HuggingFace Team. All rights reserved.
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

# TODO: Double check this
"""
Pipeline implementation and pydantic models for embedding extraction transformers
tasks
"""


from concurrent.futures import ThreadPoolExecutor
import os
from typing import Any, List, Type, Union, Tuple, Optional

import numpy
from pydantic import BaseModel, Field
from transformers.tokenization_utils_base import PaddingStrategy, TruncationStrategy

from deepsparse import Pipeline
from deepsparse.engine import Context
from deepsparse.transformers.pipelines import TransformersPipeline
from deepsparse.transformers.helpers import (
    get_onnx_path_and_configs,
    overwrite_transformer_onnx_model_inputs,
    cut_transformer_onnx_model,
)


__all__ = [
    "EmbeddingExtractionInput",
    "EmbeddingExtractionOutput",
    "EmbeddingExtractionPipeline",
]

CACHE_DIR = os.path.expanduser(os.path.join("~", ".cache", "deepsparse"))


class EmbeddingExtractionInput(BaseModel):
    """
    Schema for inputs to embedding_extraction pipelines
    """

    texts: Union[List[Tuple[str, str]], List[str]] = Field(
        description="A list of document title and content pairs, or a list of "
        "document contents"
    )


class EmbeddingExtractionOutput(BaseModel):
    """
    Schema for embedding_extraction pipeline output. Values are in batch order
    """

    # TODO: better type checking
    embeddings: Union[List[Any], Any] = Field(
        description="The output of the model which is an embedded "
        "representation of the input"
    )


@Pipeline.register(
    task="embedding_extraction",
    task_aliases=[],
    default_model_path=(
        "zoo:nlp/masked_language_modeling/bert-base/pytorch/huggingface/"
        "wikipedia_bookcorpus/12layer_pruned80_quant-none-vnni"
    ),
)
class EmbeddingExtractionPipeline(TransformersPipeline):
    def __init__(
        self,
        *,
        show_progress_bar: bool = False,
        context: Optional[Context] = None,
        **kwargs,
    ):
        self._show_progress_bar = show_progress_bar

        if context is None:
            # num_streams is arbitrarily chosen to be any value >= 2
            context = Context(num_cores=None, num_streams=2)
            kwargs.update({"context": context})

            self._thread_pool = ThreadPoolExecutor(
                max_workers=context.num_streams or 2,
                thread_name_prefix="deepsparse.pipelines.zero_shot_text_classifier",
            )

        super().__init__(**kwargs)

    @staticmethod
    def should_bucket(*args, **kwargs) -> bool:
        return False

    @staticmethod
    def create_pipeline_buckets(*args, **kwargs) -> List[Pipeline]:
        pass

    @staticmethod
    def route_input_to_bucket(
        *args, input_schema: BaseModel, pipelines: List[Pipeline], **kwargs
    ) -> Pipeline:
        pass

    @property
    def input_schema(self) -> Type[BaseModel]:
        """
        :return: pydantic model class that inputs to this pipeline must comply to
        """
        return EmbeddingExtractionInput

    @property
    def output_schema(self) -> Type[BaseModel]:
        """
        :return: pydantic model class that outputs of this pipeline must comply to
        """
        return EmbeddingExtractionOutput

    def setup_onnx_file_path(self) -> str:
        """
        Performs setup done in parent class as well as cutting the model to an
        intermediate layer for latent space comparison

        :return: file path to the processed ONNX file for the engine to compile
        """
        onnx_path = super().setup_onnx_file_path()

        (
            onnx_path,
            self.onnx_output_names,
            self._temp_model_directory,
        ) = cut_transformer_onnx_model(onnx_path)

        return onnx_path

    def parse_inputs(self, *args, **kwargs) -> BaseModel:
        if args and kwargs:
            raise ValueError(
                f"{self.__class__} only support args OR kwargs. Found "
                f" {len(args)} args and {len(kwargs)} kwargs"
            )

        if args:
            if len(args) == 1:
                # passed input_schema schema directly
                if isinstance(args[0], self.input_schema):
                    return args[0]
                return self.input_schema(texts=args[0])
            else:
                return self.input_schema(texts=args)

        return self.input_schema(**kwargs)

    def process_inputs(self, inputs: EmbeddingExtractionInput) -> List[numpy.ndarray]:
        if any([isinstance(input, List) for input in inputs]):
            # TODO: For now, strip away titles
            inputs = [input[1] for input in inputs]

        tokens = self.tokenizer(
            inputs.texts,
            add_special_tokens=True,
            return_tensors="np",
            padding=PaddingStrategy.MAX_LENGTH.value,
            truncation=TruncationStrategy.LONGEST_FIRST.value,
        )

        """
        postprocessing_kwargs = dict(
            sequences=sequences,
            labels=labels,
            multi_class=multi_class,
        )
        """
        return self.tokens_to_engine_input(tokens)

    def engine_forward(self, engine_inputs: List[numpy.ndarray]) -> List[numpy.ndarray]:

        def _engine_forward(batch_index: int, batch_origin: int):
            labelwise_inputs = engine_inputs_numpy[
                :, batch_origin : batch_origin + self._batch_size, :
            ]
            labelwise_inputs = [labelwise_input for labelwise_input in labelwise_inputs]
            engine_output = self.engine(labelwise_inputs)
            engine_outputs[batch_index] = engine_output

        engine_inputs_numpy = numpy.array(engine_inputs)
        engine_outputs = [
            None for _ in range(engine_inputs_numpy.shape[1] // self._batch_size)
        ]

        futures = [
            self._thread_pool.submit(_engine_forward, batch_index, batch_origin)
            for batch_index, batch_origin in enumerate(
                range(0, engine_inputs_numpy.shape[1], self._batch_size)
            )
        ]
        wait(futures)

        return engine_outputs

    def process_engine_outputs(self, engine_outputs: List[numpy.ndarray]) -> BaseModel:
        engine_outputs = [engine_output.T for engine_output in engine_outputs[0]]
        print(numpy.array(engine_outputs).shape)
        return self.output_schema(embeddings=engine_outputs)

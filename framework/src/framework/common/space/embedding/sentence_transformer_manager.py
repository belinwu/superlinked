# Copyright 2024 Superlinked, Inc.
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


import numpy as np
import structlog
from beartype.typing import Sequence
from numpy import ndarray
from PIL.Image import Image
from typing_extensions import override

from superlinked.framework.common.dag.context import ExecutionContext
from superlinked.framework.common.data_types import Vector
from superlinked.framework.common.space.embedding.exception import EmbeddingException
from superlinked.framework.common.space.embedding.model_manager import ModelManager
from superlinked.framework.common.space.embedding.sentence_transformer import (
    SentenceTransformer,
)
from superlinked.framework.common.space.embedding.sentence_transformer_model_cache import (
    SentenceTransformerModelCache,
)
from superlinked.framework.common.util.execution_timer import time_execution
from superlinked.framework.common.util.gpu_embedding_util import GpuEmbeddingUtil

logger = structlog.getLogger()


SENTENCE_TRANSFORMER_PROMPT_NAME_KWARG_KEY = "prompt_name"
QUERY_PROMPT_NAME = "query"

EXCEEDED_LENGTH_EXCEPTION_PREDICATE_BY_EXCEPTION_TYPE = {
    RuntimeError: lambda e: "The size of tensor a" in str(e) and "must match the size of tensor b" in str(e),
    ValueError: lambda e: str(e).startswith("Sequence length must be less than max_position_embeddings"),
}


class SentenceTransformerManager(ModelManager):
    @override
    @time_execution
    def _embed(self, inputs: Sequence[str | Image], context: ExecutionContext) -> list[list[float]] | list[np.ndarray]:
        model = self._get_embedding_model(len(inputs))
        prompt_name = self._calculate_prompt_name(model, context)
        try:
            embeddings = self._encode(inputs, model, prompt_name)
        except (RuntimeError, ValueError) as e:
            self.__handle_exceeded_length_exception(e, inputs)
        return embeddings.tolist()

    @time_execution
    def _encode(self, inputs: Sequence[str | Image], model: SentenceTransformer, prompt_name: str | None) -> ndarray:
        return model.encode(
            list(inputs),  # type: ignore[arg-type] # it also accepts Image
            prompt_name=prompt_name,
        )

    def __handle_exceeded_length_exception(
        self, exception: RuntimeError | ValueError, inputs: Sequence[str | Image]
    ) -> None:
        if (predicate := EXCEEDED_LENGTH_EXCEPTION_PREDICATE_BY_EXCEPTION_TYPE.get(type(exception))) and predicate(
            exception
        ):
            longest_input_len = max(len(str(x)) if isinstance(x, str) else 0 for x in inputs)
            raise EmbeddingException(
                f"Model {self._model_name} failed to encode inputs - input was too long. "
                "Try shortening the input text.\n"
                f"Longest input length: {longest_input_len} chars"
            ) from exception
        raise exception

    def embed_text(self, inputs: Sequence[str], context: ExecutionContext) -> list[Vector]:
        if not inputs:
            return []
        return [Vector(embedding) for embedding in self._embed(inputs, context)]

    @override
    def calculate_length(self) -> int:
        return SentenceTransformerModelCache.calculate_length(self._model_name, self._model_cache_dir)

    def _get_embedding_model(self, number_of_inputs: int) -> SentenceTransformer:
        device_type = GpuEmbeddingUtil.get_device_type(number_of_inputs)
        model = SentenceTransformerModelCache.initialize_model(self._model_name, device_type, self._model_cache_dir)
        return model

    def _calculate_prompt_name(self, model: SentenceTransformer, context: ExecutionContext) -> str | None:
        return (
            QUERY_PROMPT_NAME
            if QUERY_PROMPT_NAME in model.prompts and context.is_query_context
            else model.default_prompt_name
        )

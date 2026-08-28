"""Inference providers. vLLM and other engines are interchangeable HTTP backends."""

from mobyai.inference.base import (
    InferenceError,
    InferenceProvider,
    SchemaRepairExhaustedError,
    StructuredOutputError,
    UnknownProviderError,
)
from mobyai.inference.factory import InferenceRegistry, provider_from_role_config
from mobyai.inference.fake import FakeInferenceProvider, FakeInferenceServiceManager
from mobyai.inference.openai_compatible import OpenAICompatibleProvider
from mobyai.inference.service_manager import (
    GpuServiceManager,
    InferenceServiceManager,
    ProcessSpawnError,
    RoleUnhealthyError,
    ServiceManagerError,
    device_for_role,
    service_manager_from_config,
)
from mobyai.inference.structured import StructuredGenerator, parse_structured

__all__ = [
    "FakeInferenceProvider",
    "FakeInferenceServiceManager",
    "GpuServiceManager",
    "InferenceError",
    "InferenceProvider",
    "InferenceRegistry",
    "InferenceServiceManager",
    "OpenAICompatibleProvider",
    "ProcessSpawnError",
    "RoleUnhealthyError",
    "SchemaRepairExhaustedError",
    "ServiceManagerError",
    "StructuredGenerator",
    "StructuredOutputError",
    "UnknownProviderError",
    "device_for_role",
    "parse_structured",
    "provider_from_role_config",
    "service_manager_from_config",
]

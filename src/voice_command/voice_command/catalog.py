"""Compatibility import for the procedure-owned voice catalog contract."""

from procedure_spec import (
    VoiceCommandCatalog as ProcedureToolCatalog,
    load_voice_command_catalog as load_procedure_tool_catalog,
    voice_catalog_id_for as catalog_id_for,
)

__all__ = [
    "ProcedureToolCatalog",
    "catalog_id_for",
    "load_procedure_tool_catalog",
]

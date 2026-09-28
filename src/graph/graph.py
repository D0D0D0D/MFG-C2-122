"""Outer AgentBaseGraph for MFG-C2-122."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import yaml

from framework.graph.agent_base_graph import AgentBaseGraph

from src.nodes.main_node import SupplierCapaReviewGraphNode
from src.nodes.post_process_node import SupplierCapaPostProcessNode
from src.nodes.pre_process_node import PreProcessNode
from src.schemas.state import State
from src.services import wire_services


def load_runtime_config() -> dict[str, Any]:
    """Read the canonical runtime config without adding a dependency seam."""

    path = Path(__file__).resolve().parents[2] / "config" / "config.yaml"
    value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(value, dict):
        raise ValueError("config/config.yaml must contain a mapping")
    return value


class SupplierCorrectiveActionEvidenceBriefAgent(AgentBaseGraph):
    """Five-node backbone with a cached Cat 2 domain workflow in main."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        merged = load_runtime_config()
        if config:
            merged.update(config)
        merged.setdefault("services", wire_services())
        super().__init__(merged)

    @property
    def name(self) -> str:
        return "supplier-corrective-action-evidence-brief"

    @property
    def state_schema(self) -> type:
        return State

    def register_nodes(self) -> None:
        super().register_nodes()
        self._nodes["pre_process"] = PreProcessNode()
        self._nodes["main"] = SupplierCapaReviewGraphNode(config=self.config)
        self._nodes["post_process"] = SupplierCapaPostProcessNode(
            advisory_disclaimer_text=self.config["advisory_disclaimer_text"]
        )

    def get_output(self, state: dict[str, Any]) -> dict[str, Any]:
        output = cast(dict[str, Any], super().get_output(state))
        if state.get("error_code"):
            output["error_code"] = state["error_code"]
        if state.get("error_type"):
            output["error_type"] = state["error_type"]
        if state.get("error_log"):
            output["error_log"] = list(dict.fromkeys(state["error_log"]))
        return output


# Backward-compatible registry alias used by older scaffold tooling.
Graph = SupplierCorrectiveActionEvidenceBriefAgent

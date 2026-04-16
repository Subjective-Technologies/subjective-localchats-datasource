import os
from typing import Any

from subjective_abstract_data_source_package import SubjectiveDataSource
from brainboost_data_source_logger_package.BBLogger import BBLogger


class SubjectiveLocalchatsDataSource(SubjectiveDataSource):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        conn = getattr(self, "_connection", {}) or {}
        self.example_connection_value = conn.get("example_connection_value") or self.params.get(
            "example_connection_value", ""
        )

    @classmethod
    def connection_schema(cls) -> dict:
        return {
            "example_connection_value": {
                "type": "text",
                "label": "Example Connection Value",
                "description": "Replace this with datasource-specific connection fields.",
                "required": False,
                "placeholder": "Customize the scaffold for your datasource",
            }
        }

    @classmethod
    def request_schema(cls) -> dict:
        return {
            "text": {
                "type": "text",
                "label": "Text",
                "description": "Example per-request input. Replace or remove it as needed.",
                "required": False,
                "placeholder": "Enter request data",
            }
        }

    @classmethod
    def output_schema(cls) -> dict:
        return {
            "result": {
                "type": "text",
                "label": "Result",
                "description": "Example output field returned by run().",
            }
        }

    @classmethod
    def icon(cls) -> str:
        icon_path = os.path.join(os.path.dirname(__file__), "icon.svg")
        try:
            with open(icon_path, "r", encoding="utf-8") as handle:
                return handle.read()
        except Exception as exc:
            BBLogger.log(f"Error reading icon file: {exc}")
            return ""

    def run(self, request: dict) -> Any:
        request = request or {}
        text = (
            request.get("text")
            or self.example_connection_value
            or "Replace run() with your datasource logic."
        )
        return {"result": text}

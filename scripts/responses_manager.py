import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

class ResponsesManager:
    def __init__(self, responses_dir: Path, case_id: str):
        self.path = responses_dir / case_id / "responses.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> Dict[str, Any]:
        if not self.path.exists():
            return {}
        return json.loads(self.path.read_text(encoding="utf-8"))

    def save(self, data: Dict[str, Any]):
        self.path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def get(self, req_id: str) -> Dict[str, Any]:
        return self.load().get(req_id, {})

    def update(self, req_id: str, **kwargs):
        data = self.load()
        if req_id not in data:
            data[req_id] = {}
        data[req_id].update(kwargs)
        data[req_id]["updated_at"] = datetime.now().isoformat(timespec="seconds")
        self.save(data)

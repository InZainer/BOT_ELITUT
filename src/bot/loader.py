from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional
import yaml

@dataclass
class House:
    id: str
    name: str
    concierge_text: Optional[str] = None

@dataclass
class Guide:
    id: str
    title: str
    content_md: str

@dataclass
class Activity:
    id: str
    title: str
    description_md: str
    link_guide_id: Optional[str] = None
    links: Optional[list[str]] = None
    photos: Optional[list[str]] = None
    months: Optional[list[int]] = None  # 1..12

    def to_markdown(self) -> str:
        parts = [f"*{self.title}*", self.description_md]
        if self.link_guide_id:
            parts.append(f"Связано: {self.link_guide_id}")
        if self.links:
            parts.append("Ссылки:\n" + "\n".join(f"- {u}" for u in self.links))
        return "\n\n".join([p for p in parts if p])

class ContentLoader:
    def __init__(self, base_path: Path):
        self.base = base_path

    def _house_dir(self, house_id: str) -> Path:
        return self.base / house_id

    def load_house(self, house_id: str) -> Optional[House]:
        p = self._house_dir(house_id) / "house.yaml"
        if not p.exists():
            return None
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        return House(id=house_id, name=data.get("name", house_id), concierge_text=data.get("concierge_text"))

    def read_markdown(self, house_id: str, rel_path: str) -> str:
        p = self._house_dir(house_id) / rel_path
        if not p.exists():
            return "Пока пусто."
        return p.read_text(encoding="utf-8")

    def list_guides(self, house_id: str) -> List[Guide]:
        d = self._house_dir(house_id) / "guides"
        res: List[Guide] = []
        if d.exists():
            for p in sorted(d.glob("*.md")):
                gid = p.stem
                title = gid.replace("_", " ").title()
                res.append(Guide(id=gid, title=title, content_md=p.read_text(encoding="utf-8")))
        return res

    def get_guide(self, house_id: str, guide_id: str) -> Optional[Guide]:
        p = self._house_dir(house_id) / "guides" / f"{guide_id}.md"
        if not p.exists():
            return None
        title = guide_id.replace("_", " ").title()
        return Guide(id=guide_id, title=title, content_md=p.read_text(encoding="utf-8"))

    def list_activities(self, house_id: str) -> List[Activity]:
        p = self._house_dir(house_id) / "activities.yaml"
        if not p.exists():
            return []
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or []
        res: List[Activity] = []
        for item in data:
            res.append(Activity(
                id=str(item.get("id")),
                title=item.get("title", ""),
                description_md=item.get("description_md", ""),
                link_guide_id=item.get("link_guide_id"),
                links=item.get("links"),
                photos=item.get("photos"),
                months=item.get("months"),
            ))
        return res

    def get_activity(self, house_id: str, activity_id: str) -> Optional[Activity]:
        for a in self.list_activities(house_id):
            if a.id == activity_id:
                return a
        return None


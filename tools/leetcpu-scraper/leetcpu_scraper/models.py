"""数据模型：把抽取结果收敛成稳定、可序列化的结构。"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Problem:
    id: Any = None
    slug: str = ""
    title: str = ""
    difficulty: str = ""
    category: str = ""
    tags: list[str] = field(default_factory=list)
    sim_config: str = ""
    estimated_time: str = ""
    description: str = ""
    prompt: str = ""
    expected_output: str = ""
    constraints: list[str] = field(default_factory=list)
    examples: list[dict] = field(default_factory=list)
    targets: list[dict] = field(default_factory=list)
    has_solution: bool = False
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_raw(cls, d: dict) -> "Problem":
        return cls(
            id=d.get("id"),
            slug=d.get("slug", "") or "",
            title=d.get("title", "") or "",
            difficulty=d.get("difficulty", "") or "",
            category=d.get("category", "") or "",
            tags=list(d.get("tags") or []),
            sim_config=d.get("simConfig", "") or "",
            estimated_time=d.get("estimatedTime", "") or "",
            description=d.get("description", "") or "",
            prompt=d.get("prompt", "") or "",
            expected_output=d.get("expectedOutput", "") or "",
            constraints=[str(c) for c in (d.get("constraints") or [])],
            examples=list(d.get("examples") or []),
            targets=list(d.get("targets") or []),
            has_solution=bool(d.get("solutionCode")),
            raw=d,
        )

    def flat(self) -> dict[str, Any]:
        """扁平化，供 CSV / 表格使用。"""
        return {
            "id": self.id,
            "slug": self.slug,
            "title": self.title,
            "difficulty": self.difficulty,
            "category": self.category,
            "tags": " | ".join(map(str, self.tags)),
            "sim_config": self.sim_config,
            "estimated_time": self.estimated_time,
            "n_constraints": len(self.constraints),
            "n_examples": len(self.examples),
            "n_targets": len(self.targets),
            "targets": " | ".join(
                f"{t.get('label') or t.get('key')}{t.get('op', '')}{t.get('value')}{t.get('unit', '')}"
                for t in self.targets if isinstance(t, dict)
            ),
            "has_solution": self.has_solution,
            "url": f"https://www.leetcpu.com/#/problem/{self.slug}" if self.slug else "",
            "description": self.description,
        }


@dataclass
class Snapshot:
    url: str
    fetched_at: str
    mode: str
    static_meta: dict = field(default_factory=dict)
    rendered: dict = field(default_factory=dict)
    datasets: dict = field(default_factory=dict)
    problems: list[Problem] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    stats: dict = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["problems"] = [p.flat() for p in self.problems]
        d["problems_full"] = [p.raw for p in self.problems]
        return d

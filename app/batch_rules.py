"""积分批次选择引擎（纯函数）。

设计目标：
- 纯函数：不访问数据库，输入候选批次快照与需求，输出选批结果，便于单测与历史重放；
- 版本化：选批规则按版本注册，规则换版后仍可用旧版本重放历史选择；
- 可解释：输出每个候选的排序位次与每个选中行的入选理由；
- 只从「可用余额(remaining)」中选择，已冻结给未完成交易的数量不会出现在候选中，
  因此规则本身即保证预留数量不被误伤。
"""
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import List, Dict, Callable, Optional, Any

EPS = 0.01

DEFAULT_RULE_VERSION = "v1"

# 取得方式的中文说明
METHOD_LABELS = {
    "annual_issue": "当年核发",
    "carryover": "历年结转",
    "market_purchase": "市场购入",
    "return": "退回恢复",
}


@dataclass
class BatchCandidate:
    """选批候选（可由批次对象或历史快照构造）。"""
    batch_id: int
    batch_no: str
    source_year: int
    origin_year: int
    acquisition_method: str
    remaining: float
    valid_from: Optional[datetime] = None
    valid_until: Optional[datetime] = None
    status: str = "active"
    rule_version: str = DEFAULT_RULE_VERSION

    def to_snapshot(self) -> Dict[str, Any]:
        data = asdict(self)
        for key in ("valid_from", "valid_until"):
            value = data.get(key)
            if isinstance(value, datetime):
                data[key] = value.isoformat()
        return data

    @classmethod
    def from_snapshot(cls, data: Dict[str, Any]) -> "BatchCandidate":
        data = dict(data)
        for key in ("valid_from", "valid_until"):
            value = data.get(key)
            if isinstance(value, str):
                data[key] = datetime.fromisoformat(value)
        return cls(**data)


@dataclass
class SelectionLine:
    batch_id: int
    batch_no: str
    amount: float
    rank: int
    reason: str
    source_year: int
    origin_year: int
    acquisition_method: str
    valid_until: Optional[str]


@dataclass
class RankedCandidate:
    batch_id: int
    batch_no: str
    rank: int
    remaining: float
    chosen: bool
    sort_key: List[Any]
    reason: str


@dataclass
class SelectionResult:
    rule_version: str
    requested_amount: float
    selected_amount: float
    shortfall: float
    lines: List[SelectionLine] = field(default_factory=list)
    ranking: List[RankedCandidate] = field(default_factory=list)
    excluded: List[Dict[str, Any]] = field(default_factory=list)
    rule_description: str = ""

    @property
    def is_satisfied(self) -> bool:
        return self.shortfall <= EPS


# ---------------------------------------------------------------------------
# 各版本规则
# ---------------------------------------------------------------------------

def _method_rank_v1(method: str) -> int:
    # v1：同到期日、同来源年度时，历年结转/退回先于当年核发，市场购入最后
    return {
        "carryover": 0,
        "return": 0,
        "annual_issue": 1,
        "market_purchase": 2,
    }.get(method, 9)


def _method_rank_v2(method: str) -> int:
    # v2：当年核发优先，其次历年结转/退回，市场购入最后
    return {
        "annual_issue": 0,
        "carryover": 1,
        "return": 1,
        "market_purchase": 2,
    }.get(method, 9)


def _dt(value: Optional[datetime]) -> datetime:
    # 无期限约束的批次排在最后
    return value or datetime.max


def _sort_key_v1(c: BatchCandidate) -> tuple:
    # 到期日升序（先到期先用）→ 来源年度升序（先取得先用）→ 取得方式 → 批次号
    return (_dt(c.valid_until), c.source_year, _method_rank_v1(c.acquisition_method), c.batch_no)


def _sort_key_v2(c: BatchCandidate) -> tuple:
    # 到期日升序 → 取得方式（核发优先）→ 来源年度升序 → 批次号
    return (_dt(c.valid_until), _method_rank_v2(c.acquisition_method), c.source_year, c.batch_no)


RULE_DESCRIPTIONS: Dict[str, str] = {
    "v1": "到期日早的优先；同日到期时来源年度早的优先；仍相同时历年结转优先于当年核发，市场购入最后。",
    "v2": "到期日早的优先；同日到期时当年核发优先，其次历年结转，市场购入最后；仍相同时来源年度早的优先。",
}

RULE_KEY_FIELDS: Dict[str, List[str]] = {
    "v1": ["到期日↑", "来源年度↑", "取得方式(结转>核发>购入)"],
    "v2": ["到期日↑", "取得方式(核发>结转>购入)", "来源年度↑"],
}

_SORT_KEYS: Dict[str, Callable[[BatchCandidate], tuple]] = {
    "v1": _sort_key_v1,
    "v2": _sort_key_v2,
}


def available_rule_versions() -> List[str]:
    return sorted(_SORT_KEYS.keys())


def _line_reason(version: str, c: BatchCandidate, rank: int, key_index: int) -> str:
    """生成入选行的可读理由：指出真正起决定作用的排序字段。"""
    method_label = METHOD_LABELS.get(c.acquisition_method, c.acquisition_method)
    due = c.valid_until.strftime("%Y-%m-%d") if c.valid_until else "无到期限制"
    head = (
        f"规则{version}排序第{rank}位：有效期至{due}（最先到期）；"
        if key_index == 0 else
        f"规则{version}排序第{rank}位：与更前批次到期日相同，"
    )
    if version == "v1":
        if key_index == 0:
            tail = f"来源年度{c.source_year}，取得方式「{method_label}」。"
        elif key_index == 1:
            tail = f"来源年度{c.source_year}早于其他候选，取得方式「{method_label}」。"
        else:
            tail = f"来源年度相同，v1规则下「{method_label}」顺位更靠前。"
    else:
        if key_index == 0:
            tail = f"取得方式「{method_label}」，来源年度{c.source_year}。"
        elif key_index == 1:
            tail = f"v2规则下「{method_label}」顺位更靠前，来源年度{c.source_year}。"
        else:
            tail = f"到期日与取得方式相同，来源年度{c.source_year}更早。"
    return head + tail


def _decisive_key_index(version: str, ordered: List[BatchCandidate], idx: int) -> int:
    """找出本行与上一行排序键中第一个不同的字段索引，即决定性字段。"""
    if idx == 0:
        return 0
    key_fn = _SORT_KEYS[version]
    cur = key_fn(ordered[idx])
    prev = key_fn(ordered[idx - 1])
    for i, (a, b) in enumerate(zip(cur, prev)):
        if a != b:
            return i
    return 0


def select_batches(
    candidates: List[BatchCandidate],
    amount: float,
    as_of: Optional[datetime] = None,
    rule_version: str = DEFAULT_RULE_VERSION,
) -> SelectionResult:
    """按指定版本规则从候选批次中选择数量 amount。

    只选择 remaining > 0、状态 active 且在 as_of 时仍未过期的候选；
    冻结数量根本不进入候选列表，由调用方负责只传入 remaining（可用部分）。
    """
    if rule_version not in _SORT_KEYS:
        raise ValueError(f"未知的选批规则版本: {rule_version}，可选: {available_rule_versions()}")
    if amount < 0:
        raise ValueError("选择数量不能为负")

    as_of = as_of or datetime.utcnow()
    key_fn = _SORT_KEYS[rule_version]

    eligible: List[BatchCandidate] = []
    excluded: List[Dict[str, Any]] = []
    for c in candidates:
        if c.status != "active":
            excluded.append({"batch_id": c.batch_id, "batch_no": c.batch_no,
                             "reason": f"批次状态为{c.status}，不可选用"})
            continue
        if c.remaining <= EPS:
            excluded.append({"batch_id": c.batch_id, "batch_no": c.batch_no,
                             "reason": "可用余额为0"})
            continue
        if c.valid_until is not None and c.valid_until < as_of:
            excluded.append({"batch_id": c.batch_id, "batch_no": c.batch_no,
                             "reason": f"已于{c.valid_until.strftime('%Y-%m-%d')}过期",
                             "valid_until": c.valid_until.strftime("%Y-%m-%d")})
            continue
        eligible.append(c)

    ordered = sorted(eligible, key=key_fn)

    ranking: List[RankedCandidate] = []
    lines: List[SelectionLine] = []
    remaining_need = round(amount, 2)
    chosen_ids = set()

    for idx, c in enumerate(ordered):
        if remaining_need <= EPS:
            break
        take = round(min(c.remaining, remaining_need), 2)
        if take <= EPS:
            continue
        chosen_ids.add(c.batch_id)
        rank = idx + 1
        key_index = _decisive_key_index(rule_version, ordered, idx)
        reason = _line_reason(rule_version, c, rank, key_index)
        lines.append(SelectionLine(
            batch_id=c.batch_id,
            batch_no=c.batch_no,
            amount=take,
            rank=rank,
            reason=reason,
            source_year=c.source_year,
            origin_year=c.origin_year,
            acquisition_method=c.acquisition_method,
            valid_until=c.valid_until.strftime("%Y-%m-%d") if c.valid_until else None,
        ))
        remaining_need = round(remaining_need - take, 2)

    for idx, c in enumerate(ordered):
        sort_key = key_fn(c)
        ranking.append(RankedCandidate(
            batch_id=c.batch_id,
            batch_no=c.batch_no,
            rank=idx + 1,
            remaining=round(c.remaining, 2),
            chosen=c.batch_id in chosen_ids,
            sort_key=[str(k) for k in sort_key],
            reason=(
                f"有效期至{c.valid_until.strftime('%Y-%m-%d') if c.valid_until else '无到期限制'}，"
                f"来源年度{c.source_year}，"
                f"{METHOD_LABELS.get(c.acquisition_method, c.acquisition_method)}，"
                f"可用{round(c.remaining, 2)}"
            ),
        ))

    selected = round(amount - max(remaining_need, 0.0), 2)
    return SelectionResult(
        rule_version=rule_version,
        requested_amount=round(amount, 2),
        selected_amount=selected,
        shortfall=max(remaining_need, 0.0),
        lines=lines,
        ranking=ranking,
        excluded=excluded,
        rule_description=RULE_DESCRIPTIONS[rule_version],
    )

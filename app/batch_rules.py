"""积分批次选择规则引擎（纯函数模块）。

本模块不接触数据库：输入候选批次与规则参数，输出确定性的选择结果与
可解释的选择理由。同一组输入在任何时刻重放都会得到同一结果，
因此历史分配可以在规则换版后按原规则复算校验。
"""
from dataclasses import dataclass
from typing import List, Dict, Optional

# 选择策略
STRATEGY_EXPIRY_FIRST = "expiry_first"        # 先到期先核销（默认，避免过期浪费）
STRATEGY_FIFO = "fifo"                        # 先取得先核销
STRATEGY_SOURCE_PRIORITY = "source_priority"  # 按取得方式优先级

STRATEGY_LABELS = {
    STRATEGY_EXPIRY_FIRST: "先到期先核销",
    STRATEGY_FIFO: "先取得先核销",
    STRATEGY_SOURCE_PRIORITY: "按来源优先级",
}

METHOD_LABELS = {
    "issued": "当年核发",
    "carryover": "历年结转",
    "purchased": "市场购入",
}

PURPOSE_LABELS = {
    "reserve": "出售预留",
    "sale": "出售核销",
    "compliance": "履约抵偿",
    "carryover_out": "结转转出",
}

EPSILON = 0.005

DEFAULT_RULE_VERSION = "v1"
DEFAULT_RULE_NAME = "默认规则：先到期先核销"
DEFAULT_RULE_PARAMS: Dict = {
    # 来源优先级（source_priority 策略及并列时的次序）
    "source_priority": ["issued", "carryover", "purchased"],
    # 各取得方式的适用期限年限（自取得年度起算，含取得年度）；None 表示继承来源批次期限
    "validity_years": {"issued": 3, "carryover": 2, "purchased": None},
    # 各用途允许使用的取得方式
    "eligible_sources": {
        "reserve": ["issued", "carryover", "purchased"],
        "sale": ["issued", "carryover", "purchased"],
        "compliance": ["issued", "carryover", "purchased"],
        "carryover_out": ["issued", "carryover", "purchased"],
    },
}


class InsufficientCreditsError(ValueError):
    """可用批次数量不足以完成本次分配"""


@dataclass
class BatchCandidate:
    batch_id: int
    batch_no: str
    available: float
    acquisition_method: str
    source_year: int
    acquired_year: int
    expiry_year: int


@dataclass
class SelectedItem:
    batch_id: int
    batch_no: str
    amount: float
    reason: str


@dataclass
class SelectionResult:
    items: List[SelectedItem]
    explanation: str
    strategy: str


def merge_rule_params(params: Optional[Dict]) -> Dict:
    """把用户参数合并到默认参数上，保证键齐全（重放旧版本规则时同样适用）"""
    merged = {
        "source_priority": list(DEFAULT_RULE_PARAMS["source_priority"]),
        "validity_years": dict(DEFAULT_RULE_PARAMS["validity_years"]),
        "eligible_sources": {k: list(v) for k, v in DEFAULT_RULE_PARAMS["eligible_sources"].items()},
    }
    if params:
        for key, value in params.items():
            if key == "eligible_sources" and isinstance(value, dict):
                merged["eligible_sources"].update(value)
            elif key == "validity_years" and isinstance(value, dict):
                merged["validity_years"].update(value)
            elif key == "source_priority" and isinstance(value, list):
                merged["source_priority"] = list(value)
            else:
                merged[key] = value
    return merged


def _sort_key(candidate: BatchCandidate, strategy: str, source_priority: List[str]):
    if strategy == STRATEGY_FIFO:
        return (candidate.acquired_year, candidate.batch_id)
    if strategy == STRATEGY_SOURCE_PRIORITY:
        try:
            priority = source_priority.index(candidate.acquisition_method)
        except ValueError:
            priority = len(source_priority)
        return (priority, candidate.expiry_year, candidate.acquired_year, candidate.batch_id)
    # 默认：先到期先核销
    return (candidate.expiry_year, candidate.acquired_year, candidate.batch_id)


def _item_reason(candidate: BatchCandidate, strategy: str, source_priority: List[str]) -> str:
    method_label = METHOD_LABELS.get(candidate.acquisition_method, candidate.acquisition_method)
    if strategy == STRATEGY_FIFO:
        return (
            f"批次{candidate.batch_no}（{method_label}，{candidate.acquired_year}年取得）："
            f"取得时间较早，按先取得先核销选取"
        )
    if strategy == STRATEGY_SOURCE_PRIORITY:
        try:
            priority = source_priority.index(candidate.acquisition_method) + 1
        except ValueError:
            priority = len(source_priority) + 1
        return (
            f"批次{candidate.batch_no}（{method_label}）：取得方式优先级第{priority}位，"
            f"同优先级内适用期限至{candidate.expiry_year}年末较早"
        )
    return (
        f"批次{candidate.batch_no}（{method_label}，{candidate.source_year}年来源）："
        f"适用期限至{candidate.expiry_year}年末，在候选中到期较早，按先到期先核销选取"
    )


def select_batches(
    candidates: List[BatchCandidate],
    amount: float,
    *,
    strategy: str = STRATEGY_EXPIRY_FIRST,
    source_priority: Optional[List[str]] = None,
    eligible_methods: Optional[List[str]] = None,
    purpose: str = "compliance",
    rule_version: str = "",
) -> SelectionResult:
    """按规则从候选批次中确定性地选取指定数量。

    抛出 InsufficientCreditsError：候选可用总量不足。
    返回 SelectionResult：逐项的选取数量与原因，以及整体说明。
    """
    if amount <= EPSILON:
        raise ValueError("选取数量必须大于0")

    source_priority = source_priority or DEFAULT_RULE_PARAMS["source_priority"]
    eligible = set(eligible_methods) if eligible_methods else None

    pool = [
        c for c in candidates
        if c.available > EPSILON and (eligible is None or c.acquisition_method in eligible)
    ]
    pool.sort(key=lambda c: _sort_key(c, strategy, source_priority))

    total_available = round(sum(c.available for c in pool), 2)
    if total_available < amount - 0.001:
        purpose_label = PURPOSE_LABELS.get(purpose, purpose)
        raise InsufficientCreditsError(
            f"可用批次不足：{purpose_label}需要{round(amount, 2)}分，"
            f"符合条件的候选批次合计仅{total_available}分"
        )

    items: List[SelectedItem] = []
    remaining_need = round(amount, 2)
    for candidate in pool:
        if remaining_need <= EPSILON:
            break
        take = round(min(candidate.available, remaining_need), 2)
        if take <= EPSILON:
            continue
        items.append(SelectedItem(
            batch_id=candidate.batch_id,
            batch_no=candidate.batch_no,
            amount=take,
            reason=_item_reason(candidate, strategy, source_priority),
        ))
        remaining_need = round(remaining_need - take, 2)

    strategy_label = STRATEGY_LABELS.get(strategy, strategy)
    purpose_label = PURPOSE_LABELS.get(purpose, purpose)
    rule_label = f"规则{rule_version}" if rule_version else "当前规则"
    explanation = (
        f"按{rule_label}（{strategy_label}）为{purpose_label}选取批次："
        f"候选{len(pool)}个（合计可用{total_available}分），"
        f"选中{len(items)}个批次，合计{round(amount, 2)}分"
    )

    return SelectionResult(items=items, explanation=explanation, strategy=strategy)


def walk_reservation_items(
    reserve_items: List[Dict],
    amount: float,
) -> List[Dict]:
    """按预留明细顺序确定性地核销预留量（成交时按预留记录原路核销）。

    reserve_items: [{"batch_id":..., "batch_no":..., "outstanding":...}, ...] 按预留先后排序
    返回 [{"batch_id":..., "batch_no":..., "amount":...}, ...]
    """
    result = []
    need = round(amount, 2)
    for entry in reserve_items:
        if need <= EPSILON:
            break
        outstanding = round(entry.get("outstanding", 0.0), 2)
        take = round(min(outstanding, need), 2)
        if take <= EPSILON:
            continue
        result.append({
            "batch_id": entry["batch_id"],
            "batch_no": entry.get("batch_no", ""),
            "amount": take,
        })
        need = round(need - take, 2)
    if need > EPSILON:
        raise InsufficientCreditsError(
            f"预留量不足：需核销{round(amount, 2)}分，预留可核销"
            f"{round(sum(e.get('outstanding', 0.0) for e in reserve_items), 2)}分"
        )
    return result

"""配置迁移函数，用于一次性原地升级旧结构。"""

from __future__ import annotations

import json
import copy
from typing import Any, Dict

from src.utils.card_filename import normalize_card_base_name, normalize_config_key, split_enhance_key
from src.config.strategy_effects import normalize_effect_steps_to_ops


def migrate_high_priority_cards_priority_fields(config: Dict[str, Any]) -> bool:
    """将旧 ``high_priority_cards[*].priority`` 拆分为进化前后字段。

    仅存在 ``priority`` 时，将其同时写入 ``priority_pre_evolution`` 与
    ``priority_post_evolution``，随后删除旧键；只存在前后任一字段时复制到缺失
    字段；两者均存在时仅清理冗余旧键。迁移原地执行，发生修改时返回 ``True``。
    """

    high_priority_cards = config.get("high_priority_cards")
    if not isinstance(high_priority_cards, dict):
        return False

    changed = False
    for card_name, card_cfg in list(high_priority_cards.items()):
            # 极旧格式会直接用整数或字符串表示优先级。
        if isinstance(card_cfg, (int, float, str)):
            try:
                priority_value = int(card_cfg)
            except Exception:
                continue

            high_priority_cards[card_name] = {
                "priority_pre_evolution": priority_value,
                "priority_post_evolution": priority_value,
            }
            changed = True
            continue

        if not isinstance(card_cfg, dict):
            continue

        has_pre = "priority_pre_evolution" in card_cfg
        has_post = "priority_post_evolution" in card_cfg

        if ("priority" in card_cfg) and (not has_pre) and (not has_post):
            try:
                raw_priority = card_cfg.get("priority")
                if not isinstance(raw_priority, (int, float, str)):
                    continue
                priority_value = int(raw_priority)
            except Exception:
                # 无法解析时保留旧数据，避免静默丢失用户配置。
                continue

            card_cfg["priority_pre_evolution"] = priority_value
            card_cfg["priority_post_evolution"] = priority_value
            del card_cfg["priority"]
            changed = True
            continue

        # 补齐缺失阶段，确保调用方始终可以读取两个字段。
        if has_pre and not has_post:
            card_cfg["priority_post_evolution"] = card_cfg["priority_pre_evolution"]
            changed = True
        elif has_post and not has_pre:
            card_cfg["priority_pre_evolution"] = card_cfg["priority_post_evolution"]
            changed = True

        # 拆分字段齐全后删除冗余旧键。
        if ("priority" in card_cfg) and ("priority_pre_evolution" in card_cfg) and (
            "priority_post_evolution" in card_cfg
        ):
            del card_cfg["priority"]
            changed = True

    return changed


def migrate_strategy_effects_schema(config: Dict[str, Any]) -> bool:
    """根据旧字段和默认值填充或升级 ``strategy.effects``。

    目标是形成单一、结构化的效果映射；本阶段为兼容仍保留旧字段，不在此删除；
    整个过程具备幂等性，可重复执行。
    """

    changed = False

    # 确保外层容器存在且类型正确。
    strategy = config.get("strategy")
    if not isinstance(strategy, dict):
        strategy = {}
        config["strategy"] = strategy
        changed = True

    effects = strategy.get("effects")
    if not isinstance(effects, dict):
        effects = {}
        strategy["effects"] = effects
        changed = True

    # 记录迁移前是否已有任何效果。已有内容时不得重复填充默认值，否则用户删除的
    # 默认条目会在每次启动时重新出现。
    had_any_effects = bool(effects)

    def _norm_select_option(v: Any) -> int | None:
        if v in (1, "1", "选项1", "Option1", "option1"):
            return 1
        if v in (2, "2", "选项2", "Option2", "option2"):
            return 2
        return None

    def _is_select_option_step(step: Any) -> bool:
        if not isinstance(step, dict):
            return False
        if "select_option" in step:
            return True
        return str(step.get("op") or "") == "select_option"

    def _ensure_steps(card_name: str, trigger: str) -> list[Dict[str, Any]]:
        nonlocal changed
        card_eff = effects.get(card_name)
        if not isinstance(card_eff, dict):
            card_eff = {}
            effects[card_name] = card_eff
            changed = True
        steps = card_eff.get(trigger)
        if steps is None:
            steps = []
            card_eff[trigger] = steps
            changed = True
        elif not isinstance(steps, list):
            steps = []
            card_eff[trigger] = steps
            changed = True
        return steps

    def _seed_select_option_if_missing(card_name: str, trigger: str, opt: int) -> None:
        nonlocal changed
        steps = _ensure_steps(card_name, trigger)
        if any(_is_select_option_step(s) for s in steps):
            return
        steps.insert(0, {"op": "select_option", "index": int(opt)})
        changed = True

    # 旧 ``card_mode_options`` 转为 ``on_play`` 的 ``select_option``。
    legacy_mode = config.get("card_mode_options")
    if isinstance(legacy_mode, dict):
        for card_name, opt_raw in legacy_mode.items():
            opt = _norm_select_option(opt_raw)
            if opt is None:
                continue
            _seed_select_option_if_missing(str(card_name), "on_play", opt)

    # 旧进化选项转为进化与超进化触发器的 ``select_option``。
    legacy_evo_mode = config.get("card_evolve_mode_options")
    if isinstance(legacy_evo_mode, dict):
        for card_name, opt_raw in legacy_evo_mode.items():
            opt = _norm_select_option(opt_raw)
            if opt is None:
                continue
            card_name = str(card_name)
            _seed_select_option_if_missing(card_name, "on_evolve", opt)
            _seed_select_option_if_missing(card_name, "on_super_evolve", opt)

    # 填充默认特殊目标与动作，使其可由配置编辑。仅对新配置执行一次，不能在每次
    # 加载时重复填充，否则用户无法真正删除旧规则或默认规则。
    defaults_seeded_key = "_effects_defaults_seeded"
    if not bool(strategy.get(defaults_seeded_key)):
        if had_any_effects:
            # 现有配置只记录已填充标记，不改动效果内容。
            strategy[defaults_seeded_key] = True
            changed = True
        else:
            try:
                from src.config.strategy_defaults import build_default_effects

                defaults = build_default_effects()
            except Exception:
                defaults = {}

            if isinstance(defaults, dict):
                for card_name, card_eff in defaults.items():
                    if not isinstance(card_eff, dict):
                        continue
                    for trigger, default_steps in card_eff.items():
                        if not isinstance(default_steps, list):
                            continue

                        steps = _ensure_steps(str(card_name), str(trigger))
                        # 仅追加缺失步骤，保持幂等。
                        for step in default_steps:
                            if not isinstance(step, dict):
                                continue
                            if any(isinstance(s, dict) and s == step for s in steps):
                                continue
                            steps.append(step)
                            changed = True

            strategy[defaults_seeded_key] = True
            changed = True

    return changed


def migrate_strategy_effects_to_ops(config: Dict[str, Any]) -> bool:
    """将 ``strategy.effects[*][trigger]`` 升级为 Step3A 操作结构。

    迁移具备幂等性，结果只包含规范运行时结构，不保留运行时旧包装器。
    """

    strategy = config.get("strategy")
    if not isinstance(strategy, dict):
        return False
    effects = strategy.get("effects")
    if not isinstance(effects, dict):
        return False

    def _is_op(step: Any, op_id: str) -> bool:
        return isinstance(step, dict) and str(step.get("op") or "") == str(op_id)

    changed = False

    for card_name, card_eff in list(effects.items()):
        if not isinstance(card_eff, dict):
            continue

        for trigger, steps in list(card_eff.items()):
            if not isinstance(steps, list):
                continue

            try:
                new_steps = normalize_effect_steps_to_ops(steps)
            except Exception:
                new_steps = [dict(s) for s in steps if isinstance(s, dict)]

                # 保留旧进化语义：先执行动作或目标选择，再执行选项选择。
            if str(trigger) in ("on_evolve", "on_super_evolve", "on_evolve_bridge", "on_super_evolve_bridge"):
                reordered = [s for s in new_steps if not _is_op(s, "select_option")] + [
                    s for s in new_steps if _is_op(s, "select_option")
                ]
                if reordered != new_steps:
                    new_steps = reordered
                    changed = True

            # 对完全相同的字典步骤保序去重。
            deduped = []
            seen = set()
            for s in new_steps:
                if not isinstance(s, dict):
                    continue
                key = json.dumps(s, ensure_ascii=False, sort_keys=True)
                if key in seen:
                    continue
                seen.add(key)
                deduped.append(s)

            if deduped != steps:
                if deduped:
                    card_eff[trigger] = deduped
                else:
                    del card_eff[trigger]
                changed = True

        # 运行时不再让超进化回退到普通进化；兼容逻辑只在迁移边界补齐缺失触发器。
        try:
            on_evolve_steps = card_eff.get("on_evolve")
            on_super_steps = card_eff.get("on_super_evolve")
            if (
                isinstance(on_evolve_steps, list)
                and on_evolve_steps
                and (
                    not isinstance(on_super_steps, list)
                    or not on_super_steps
                )
            ):
                card_eff["on_super_evolve"] = copy.deepcopy(on_evolve_steps)
                changed = True

            on_evolve_b_steps = card_eff.get("on_evolve_bridge")
            on_super_b_steps = card_eff.get("on_super_evolve_bridge")
            if (
                isinstance(on_evolve_b_steps, list)
                and on_evolve_b_steps
                and (
                    not isinstance(on_super_b_steps, list)
                    or not on_super_b_steps
                )
            ):
                card_eff["on_super_evolve_bridge"] = copy.deepcopy(on_evolve_b_steps)
                changed = True
        except Exception:
            pass

    # 清理不再包含任何触发步骤的卡牌条目。
        if not card_eff:
            del effects[card_name]
            changed = True

    return changed


def migrate_strategy_split_attack_times_buff(config: Dict[str, Any]) -> bool:
    """将旧版混合增益步骤拆成相互独立的操作。

    旧 ``buff`` 会同时携带攻防增量与 ``attack_times``；新结构保留纯 ``buff``，
    并把攻击次数单独拆为 ``buff_attack_times``。
    """

    strategy = config.get("strategy")
    if not isinstance(strategy, dict):
        return False
    effects = strategy.get("effects")
    if not isinstance(effects, dict):
        return False

    def _safe_int(v: Any, default: int = 0) -> int:
        try:
            return int(v)
        except Exception:
            return int(default)

    changed = False

    for _card_name, card_eff in list(effects.items()):
        if not isinstance(card_eff, dict):
            continue

        for trigger, steps in list(card_eff.items()):
            if not isinstance(steps, list) or not steps:
                continue

            local_changed = False
            new_steps = []
            for step in list(steps):
                if not isinstance(step, dict):
                    continue

                if str(step.get("op") or "") != "buff" or step.get("attack_times") is None:
                    new_steps.append(copy.deepcopy(step))
                    continue

                local_changed = True
                stat_step = copy.deepcopy(step)
                attack_times_raw = stat_step.pop("attack_times", None)
                if stat_step:
                    new_steps.append(stat_step)

                target_mode = str(stat_step.get("target") or step.get("target") or "others")
                attack_times_step: Dict[str, Any] = {
                    "op": "buff_attack_times",
                    "target": target_mode,
                    "attack_times": max(1, _safe_int(attack_times_raw, 1)),
                }
                if step.get("on_error"):
                    attack_times_step["on_error"] = step.get("on_error")
                new_steps.append(attack_times_step)

            if local_changed:
                card_eff[trigger] = new_steps
                changed = True

    return changed


def prune_invalid_strategy_effect_ops(config: Dict[str, Any]) -> bool:
    """删除当前注册表不再支持的效果步骤。

    该清理特意放在配置加载与保存边界执行，使含有已移除操作的旧配置仍可加载，
    并在下次保存时自动去掉无效子句。
    """

    strategy = config.get("strategy")
    if not isinstance(strategy, dict):
        return False
    effects = strategy.get("effects")
    if not isinstance(effects, dict):
        return False

    try:
        from src.config.effects_registry import OPERATIONS, TRIGGERS
    except Exception:
        return False

    trigger_context: Dict[str, str] = {}
    for row in list(TRIGGERS or []):
        if not isinstance(row, dict):
            continue
        tid = str(row.get("id") or "")
        ctx = str(row.get("context_kind") or "")
        if tid and ctx:
            trigger_context[tid] = ctx

    op_contexts: Dict[str, set[str]] = {}
    for row in list(OPERATIONS or []):
        if not isinstance(row, dict):
            continue
        oid = str(row.get("op_id") or "")
        contexts_raw = row.get("supported_context_kinds")
        if not oid or not isinstance(contexts_raw, list):
            continue
        contexts = {str(v) for v in contexts_raw if str(v or "")}
        if contexts:
            op_contexts[oid] = contexts

    changed = False

    for card_name, card_eff in list(effects.items()):
        if not isinstance(card_eff, dict):
            del effects[card_name]
            changed = True
            continue

        for trigger, steps in list(card_eff.items()):
            trigger_id = str(trigger or "")
            context_kind = trigger_context.get(trigger_id)
            if not context_kind or not isinstance(steps, list):
                del card_eff[trigger]
                changed = True
                continue

            cleaned = []
            for step in list(steps or []):
                if not isinstance(step, dict):
                    changed = True
                    continue
                op_id = str(step.get("op") or "")
                if not op_id:
                    changed = True
                    continue
                supported_contexts = op_contexts.get(op_id)
                if not supported_contexts or context_kind not in supported_contexts:
                    changed = True
                    continue
                cleaned.append(step)

            if cleaned != steps:
                if cleaned:
                    card_eff[trigger] = cleaned
                else:
                    del card_eff[trigger]
                changed = True

        if not card_eff:
            del effects[card_name]
            changed = True

    return changed


def migrate_strategy_name_keys(config: Dict[str, Any]) -> bool:
    """将策略相关卡牌键规范为不带图片后缀的基础名称。

    处理 ``high_priority_cards``、``evolve_priority_cards`` 与
    ``strategy.effects``：移除随从 ``_<atk>_<hp>`` 身材后缀；需要时保留
    ``name@cost`` 爆能层级；进化优先级始终只按基础名称保存，不带 ``@cost``。
    """

    changed = False

    def _merge_dict(dst: Dict[str, Any], src: Dict[str, Any]) -> Dict[str, Any]:
        out = copy.deepcopy(dst)
        for k, v in src.items():
            out[k] = copy.deepcopy(v)
        return out

    def _normalize_mapping(
        mapping: Any,
        *,
        force_base_key: bool = False,
        merge_effects: bool = False,
    ) -> tuple[Dict[str, Any], bool]:
        if not isinstance(mapping, dict):
            return {}, False

        out: Dict[str, Any] = {}
        local_changed = False

        for raw_k, raw_v in mapping.items():
            if not isinstance(raw_k, str):
                continue

            nk = normalize_config_key(raw_k)
            if force_base_key:
                base, _enh = split_enhance_key(nk)
                nk = normalize_card_base_name(str(base or ""))

            if not nk:
                local_changed = True
                continue

            if nk != raw_k:
                local_changed = True

            if nk not in out:
                out[nk] = copy.deepcopy(raw_v)
                continue

            # 规范化后发生键冲突时合并内容。
            local_changed = True
            existing = out[nk]
            if merge_effects and isinstance(existing, dict) and isinstance(raw_v, dict):
                merged_eff = copy.deepcopy(existing)
                for trig, steps in raw_v.items():
                    if trig not in merged_eff:
                        merged_eff[trig] = copy.deepcopy(steps)
                        continue
                    if isinstance(merged_eff.get(trig), list) and isinstance(steps, list):
                        cur = list(merged_eff.get(trig) or [])
                        for s in steps:
                            if s in cur:
                                continue
                            cur.append(copy.deepcopy(s))
                        merged_eff[trig] = cur
                out[nk] = merged_eff
            elif isinstance(existing, dict) and isinstance(raw_v, dict):
                out[nk] = _merge_dict(existing, raw_v)
            else:
                out[nk] = copy.deepcopy(raw_v)

        return out, local_changed

    hp = config.get("high_priority_cards")
    if isinstance(hp, dict):
        hp_new, hp_changed = _normalize_mapping(hp, force_base_key=False, merge_effects=False)
        if hp_changed:
            config["high_priority_cards"] = hp_new
            changed = True

    ep = config.get("evolve_priority_cards")
    if isinstance(ep, dict):
        ep_new, ep_changed = _normalize_mapping(ep, force_base_key=True, merge_effects=False)
        if ep_changed:
            config["evolve_priority_cards"] = ep_new
            changed = True

    strategy = config.get("strategy")
    if isinstance(strategy, dict):
        effects = strategy.get("effects")
        if isinstance(effects, dict):
            eff_new, eff_changed = _normalize_mapping(effects, force_base_key=False, merge_effects=True)
            if eff_changed:
                strategy["effects"] = eff_new
                changed = True

    return changed


def migrate_runtime_legacy_fields(config: Dict[str, Any]) -> bool:
    """一次性迁移填充完成后删除运行时旧字段。

    兼容性仅保留在启动和加载边界，运行时只消费规范结构与规范路径。
    """

    if not isinstance(config, dict):
        return False

    changed = False

    for key in ("card_mode_options", "card_evolve_mode_options"):
        if key in config:
            del config[key]
            changed = True

    game = config.get("game")
    if isinstance(game, dict) and "use_enhanced_mulligan" in game:
        del game["use_enhanced_mulligan"]
        changed = True

    return changed

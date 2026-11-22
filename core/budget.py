"""每日预算闸。

用户要的是「每天花超一定金额后，自动把能花钱的功能都收一收」。

降级对象各不相同，所以这里只做判定，由各个消耗点自己来问：

- 学习（后台提炼）：整个停掉
- 主动回复：停
- 表情包：marker / analyze 退回 keyword——还发图，但不花「判断该不该发」的钱
- 联网搜索：工具不注入
- 画像生成：停

**为什么不塞进 Settings.get**：藏在取值层里会让「今天为什么什么都没发生」
变成一个极难排查的问题（每个开关看起来都是开的）。让每个消耗点显式问一句，
翻代码就知道边界在哪，面板上也留了状态可看。

**按天刷新怎么做的**：不额外开定时器。用量本来就是一天一个文件，
跨过零点后 ``today_cost()`` 读的是新文件，自然归零。

**只有「可能产生新请求」的功能被拦**。消息收集、标签写入、表情包本地判定
这些都不花 token，照常跑——不然恢复正常后历史会断一截。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from astrbot.api import logger

# 插件里走包相对导入；dev 下的断言脚本按文件路径加载模块时没有父包，
# 退回绝对导入。这样同一份判定逻辑在两种跑法下都能测
try:
    from .usage import today_key
except ImportError:  # pragma: no cover - 只在测试脚本里发生
    from usage import today_key

# 花费缓存几秒。主链路一轮会问好几次（学习一次、主动回复一次、
# 表情包两次），每次都去拿 usage 的锁没必要。
CACHE_SECONDS = 2.0


class Budget:
    def __init__(self, plugin) -> None:
        self.plugin = plugin
        self._at = 0.0
        self._cost = 0.0
        self._hits = 0
        # 「今天先放开」只记日期：第二天 today_key() 变了，判断自动失效。
        # 落盘是为了重启后仍然算数——用户点了放开，重启又被卡住会很恼人
        self.path: Path = Path(plugin.data_dir) / "budget.json"
        self._bypass_day = self._load_bypass()

    # ── 手动放开（只到当天）────────────────────────────────────────────

    def _load_bypass(self) -> str:
        try:
            if not self.path.is_file():
                return ""
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return str((data or {}).get("bypass_day") or "")
        except (OSError, json.JSONDecodeError, TypeError) as e:
            logger.debug(f"[群聊记忆] 读取预算放开状态失败: {e}")
            return ""

    def _save_bypass(self) -> None:
        try:
            self.path.write_text(
                json.dumps({"bypass_day": self._bypass_day}, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError as e:
            logger.warning(f"[群聊记忆] 保存预算放开状态失败: {e}")

    def bypassed_today(self) -> bool:
        """今天是否被手动放开过。跨零点自动变回否。"""
        return bool(self._bypass_day) and self._bypass_day == today_key()

    def release_today(self) -> str:
        """今天先放开。不动机器人的任何配置，只是让闸门今天不拦。"""
        self._bypass_day = today_key()
        self._save_bypass()
        return self._bypass_day

    def relock(self) -> None:
        """收回放开，立刻恢复按上限拦。"""
        self._bypass_day = ""
        self._save_bypass()

    def _cfg(self) -> tuple[bool, float]:
        try:
            on = bool(self.plugin.settings.get("daily_budget_enabled"))
            limit = float(self.plugin.settings.get("daily_budget_yuan") or 0)
        except Exception:  # noqa: BLE001 - 老配置没有这两项时当作关
            return False, 0.0
        return on, max(0.0, limit)

    def spent(self) -> float:
        """今天已经花了多少（元）。"""
        now = time.time()
        if now - self._at < CACHE_SECONDS:
            return self._cost
        try:
            value = float(self.plugin.usage.today_cost())
        except Exception as e:  # noqa: BLE001 - 读不到就当没花，别把功能锁死
            logger.debug(f"[群聊记忆] 读取今日花费失败: {e}")
            value = 0.0
        self._at = now
        self._cost = value
        return value

    def exceeded(self) -> bool:
        """今天是否已经花到上限。开关关着时永远 False。"""
        on, limit = self._cfg()
        if not on or limit <= 0:
            return False
        if self.bypassed_today():
            return False
        if self.spent() < limit:
            return False
        self._hits += 1
        return True

    def snapshot(self) -> dict:
        """面板用。限额外加今日花费和剩余。"""
        on, limit = self._cfg()
        cost = self.spent()
        capped = bool(on and limit > 0)
        bypass = self.bypassed_today()
        return {
            "enabled": on,
            "limit": round(limit, 2),
            "spent": round(cost, 4),
            # exceeded 是「闸门会不会拦」，手动放开之后就是 False，
            # 面板要据此把按钮换成「恢复限制」
            "exceeded": bool(capped and cost >= limit and not bypass),
            "over_limit": bool(capped and cost >= limit),
            "remain": round(max(0.0, limit - cost), 4) if capped else 0.0,
            "bypassed": bypass,
            "bypass_day": self._bypass_day,
            "blocked": self._hits,
        }

"""成长任务的「难度分级 + 完成策略」。

这里沉淀的是实测结论，不是猜测：

  已实测可自动完成的（eventCode 经账号实测命中）：
    chat_5               <- chat_request_send
    automation_1         <- automated_task_create_suc
    skill_1              <- skill_info
    RichMeow_Chat        <- chat_request_send（桌面端对话，实测同链路）

  已实测「发事件包无效」的（枚举 1131 个候选事件均未命中）：
    Model_chat_GLM5.2 / expert_5 / Hp_Appearance
    —— 归为 MANUAL，面板上标灰但仍允许「尝试」，便于日后发现新方法。

  奖励为 0 或依赖支付/登录/三方的：直接跳过。

分级说明：
  AUTO   一次事件包即可完成（简单，优先执行）
  MULTI  需要重复 N 次事件包（如 chat_5 需 5 次）
  MANUAL 暂未找到自动化方式（需人工在客户端/网页操作）
  SKIP   明确不处理（奖励为 0、需支付、需三方授权等）
"""
from dataclasses import dataclass, field

# 难度等级
AUTO = "auto"        # 可直接自动完成
MULTI = "multi"      # 需重复触发
MANUAL = "manual"    # 暂不可自动
SKIP = "skip"        # 跳过

LEVEL_LABEL = {
    AUTO: "简单",
    MULTI: "简单(多次)",
    MANUAL: "复杂",
    SKIP: "跳过",
}


@dataclass
class TaskPlan:
    """单个任务的完成策略。"""
    code: str
    level: str
    event_codes: list[str] = field(default_factory=list)
    reason: str = ""
    # 需要触发的次数；None 表示按任务 target 动态决定
    times: int | None = None

    @property
    def actionable(self) -> bool:
        """是否可由本系统自动完成。"""
        return self.level in (AUTO, MULTI) and bool(self.event_codes)


#: 任务策略表。键为 task_code。
#: 未列出的任务默认按 MANUAL 处理（宁可让管理员手动，也不盲目发包）。
TASK_PLANS: dict[str, TaskPlan] = {
    # ---------------- 已实测可自动 ----------------
    "chat_5": TaskPlan(
        "chat_5", MULTI, ["chat_request_send"],
        "对话类：发送带 growthEvent 的对话请求，按 target 次数重复",
    ),
    "RichMeow_Chat": TaskPlan(
        "RichMeow_Chat", AUTO, ["chat_request_send"],
        "桌面端对话：与对话类同链路（实测桌面端对话即计数）",
    ),
    "automation_1": TaskPlan(
        "automation_1", AUTO, ["automated_task_create_suc"],
        "自动化任务：创建成功事件（已实测命中）",
    ),
    "skill_1": TaskPlan(
        "skill_1", AUTO, ["skill_info"],
        "尝鲜技能：skill_info 事件（已实测命中）",
    ),

    # ---------------- 已实测发事件包无效 ----------------
    "Model_chat_GLM5.2": TaskPlan(
        "Model_chat_GLM5.2", MANUAL, [],
        "模型体验：枚举 1131 个候选事件未命中，可能需真实调用该模型",
    ),
    "expert_5": TaskPlan(
        "expert_5", MANUAL, [],
        "召唤专家：未找到有效事件，可能需真实专家会话",
    ),
    "expert_5_paid": TaskPlan(
        "expert_5_paid", MANUAL, [],
        "召唤专家（付费版）：同上",
    ),
    "Expert_team_use_3": TaskPlan(
        "Expert_team_use_3", MANUAL, [],
        "召唤专家团：同上",
    ),
    "Hp_Appearance": TaskPlan(
        "Hp_Appearance", MANUAL, [],
        "和平精英主题：未找到有效事件",
    ),
    "template_5": TaskPlan(
        "template_5", MANUAL, ["agent_task_created_with_template"],
        "使用模板：尝试模板事件，未验证",
    ),
    "playbook_prompt": TaskPlan(
        "playbook_prompt", MANUAL, [],
        "灵感案例：未找到有效事件",
    ),
    "Library_read": TaskPlan(
        "Library_read", MANUAL, [],
        "资料库：未找到有效事件",
    ),
    "Expert_Philanthropy": TaskPlan(
        "Expert_Philanthropy", MANUAL, [],
        "公益专家：未找到有效事件",
    ),
    "create_canvas": TaskPlan(
        "create_canvas", MANUAL, [],
        "设计创意模式：需真实创建画布",
    ),
    "Buddy_App": TaskPlan(
        "Buddy_App", MANUAL, [],
        "发现应用：未找到有效事件",
    ),
    "Buddy_App_QQ": TaskPlan(
        "Buddy_App_QQ", MANUAL, [],
        "企鹅教师助手：未找到有效事件",
    ),
    "Expert_lighthouse": TaskPlan(
        "Expert_lighthouse", MANUAL, [],
        "腾讯轻量云专家：未找到有效事件",
    ),

    # ---------------- 明确跳过 ----------------
    "black_cat": TaskPlan(
        "black_cat", SKIP, [],
        "奖励为 0（夜猫子折扣活动），无收益",
    ),
}


def plan_for(task_code: str) -> TaskPlan:
    """取任务策略；未登记的任务按 MANUAL 处理。"""
    return TASK_PLANS.get(task_code) or TaskPlan(
        task_code, MANUAL, [], "未登记的任务类型，需人工确认"
    )


def classify(task: dict) -> dict:
    """给任务补充分级信息，供前端展示。"""
    code = task.get("task_code") or ""
    plan = plan_for(code)
    reward = task.get("reward_credit") or 0

    level = plan.level
    # 奖励为 0 的一律降级为跳过（策略表可能滞后于上游活动）
    if reward <= 0 and level != SKIP:
        level = SKIP

    progress = task.get("progress") or {}
    target = progress.get("target")
    current = progress.get("current") or 0

    return {
        **task,
        "level": level,
        "level_label": LEVEL_LABEL.get(level, "未知"),
        "actionable": level in (AUTO, MULTI) and bool(plan.event_codes),
        "strategy": plan.reason,
        # 待触发次数：优先用任务自带 target，否则用策略表，最后兜底 1
        "need_times": (target - current) if isinstance(target, int) and target > current
                      else (plan.times or 1),
    }

"""成长任务的「难度分级 + 完成策略」。

这里沉淀的是实测结论，不是猜测：

  已实测可自动完成的（走 chat/completions 的 growthEvent）：
    chat_5               <- chat_request_send
    automation_1         <- automated_task_create_suc
    skill_1              <- skill_info
    Model_chat_GLM5.2    <- 请求体 model 必须真的是 glm-5.2

  已实测可自动完成的（走 POST /v2/report 上报真实业务事件）：
    Library_read         <- web 域 web_element_click（+100 已实测到账）
    playbook_prompt      <- billing 域 playbook_prompt_send（+100 已实测到账）
    —— 这两个不吃 growthEvent，必须带完整业务字段与对应域的指纹头，
       走 chat 事件包无论发多少次都不计数。

  已实测「发事件包无效」的（枚举 1131 个候选事件均未命中）：
    expert_5 / Hp_Appearance / template_5 / Buddy_App / Expert_lighthouse
    Expert_team_use_3 / create_canvas / RichMeow_Chat
    —— 归为 MANUAL。部分任务参考资料显示需上报真实业务事件，但事件体里
       要填真实对象 id（专家 id / 模板 id / 画布 id），自造 id 属于伪造
       业务对象，后端核对即露，故暂不纳入自动执行。

  奖励为 0 或依赖支付/登录/三方的：直接跳过。
    Expert_Philanthropy 需真实捐款；black_cat 奖励为 0。

分级说明：
  AUTO   一次事件即可完成（简单，优先执行）
  MULTI  需要重复 N 次（如 chat_5 需 5 次）
  MANUAL 暂未找到安全的自动化方式（需人工在客户端/网页操作）
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
    # 指定模型：非空时用该模型真实调用（用于「体验某模型」类任务）
    model: str = ""
    # 触发方式：空 = 走 chat/completions 的 growthEvent；
    # 非空 = 调用 AccountSession 上的同名方法（如 fire_library_read）。
    # 有些任务不吃 growthEvent，要求客户端上报真实业务事件，必须分开走。
    firer: str = ""

    @property
    def actionable(self) -> bool:
        """是否可由本系统自动完成。

        两种触发方式都算「可自动」：
          * 有 event_codes（走 chat/completions 的 growthEvent）
          * 有 firer（走 POST /v2/report 上报真实业务事件）
        """
        return self.level in (AUTO, MULTI) and bool(self.event_codes or self.firer)


#: 任务策略表。键为 task_code。
#: 未列出的任务默认按 MANUAL 处理（宁可让管理员手动，也不盲目发包）。
TASK_PLANS: dict[str, TaskPlan] = {
    # ---------------- 已实测可自动 ----------------
    "chat_5": TaskPlan(
        "chat_5", MULTI, ["chat_request_send"],
        "对话类：发送带 growthEvent 的对话请求，按 target 次数重复",
    ),
    "automation_1": TaskPlan(
        "automation_1", AUTO, ["automated_task_create_suc"],
        "自动化任务：创建成功事件（已实测命中）",
    ),
    "skill_1": TaskPlan(
        "skill_1", AUTO, ["skill_info"],
        "尝鲜技能：skill_info 事件（已实测命中）",
    ),

    # ---------------- 已实测可自动（需指定模型）----------------
    "Model_chat_GLM5.2": TaskPlan(
        "Model_chat_GLM5.2", AUTO, ["chat_request_send"],
        "模型体验：请求体 model 必须真的是 glm-5.2（发事件包无效，已实测）",
        model="glm-5.2",
    ),

    # ---------------- 已实测发事件包无效 ----------------
    "RichMeow_Chat": TaskPlan(
        "RichMeow_Chat", MANUAL, [],
        "桌面端对话：实测 chat_request_send 等 5 个事件均不计数，"
        "上游按客户端类型判定，需真实桌面端对话",
    ),
    "expert_5": TaskPlan(
        "expert_5", MANUAL, [],
        "召唤专家：召唤=本地专家包的下载+激活（ExpertSummonService），"
        "非服务端事件；埋点 expert_summoned 不驱动进度，后端无 summon 接口",
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
        "和平精英主题：需桌面端「菜单-外观」切换主题，纯客户端本地状态",
    ),
    "template_5": TaskPlan(
        "template_5", MANUAL, ["agent_task_created_with_template"],
        "使用模板：尝试模板事件，未验证",
    ),
    # 已实测可自动完成（走 POST /v2/report 上报真实业务事件，
    # 不是 growthEvent —— 这两个任务不吃事件包）
    "playbook_prompt": TaskPlan(
        "playbook_prompt", AUTO, [],
        "灵感案例：billing 域上报 playbook_prompt_send（已实测点亮 +100）",
        firer="fire_playbook_prompt",
    ),
    "Library_read": TaskPlan(
        "Library_read", AUTO, [],
        "资料库：web 域上报资料库介绍页点击（已实测点亮 +100）",
        firer="fire_library_read",
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
        # 直接用 TaskPlan.actionable，不要再重复写一遍判断条件——
        # 之前这里单独判了 event_codes，导致「用 firer 上报」的任务
        # 明明能自动完成，面板上却显示不可自动
        "actionable": level in (AUTO, MULTI) and bool(plan.event_codes or plan.firer),
        "strategy": plan.reason,
        # 待触发次数：优先用任务自带 target，否则用策略表，最后兜底 1
        "need_times": (target - current) if isinstance(target, int) and target > current
                      else (plan.times or 1),
    }

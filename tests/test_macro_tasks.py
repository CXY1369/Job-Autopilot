from autojobagent.core.macro_tasks import build_macro_tasks
from autojobagent.core.semantic_tree import OptionNode, QuestionBlock


def _qb(question: str, options: list[str], role: str = "button") -> QuestionBlock:
    return QuestionBlock(
        question_id="q1",
        question_text=question,
        control_type="single_choice",
        required=True,
        has_error=False,
        options=[
            OptionNode(text=opt, role=role, selected=False, ref_id=f"e{i+1}")
            for i, opt in enumerate(options)
        ],
        selected_options=[],
    )


def test_general_mapper_handles_non_yes_no_boolean_wording():
    profile = {
        "work_authorization": {
            "authorized_to_work_in_us": True,
            "require_visa_sponsorship": False,
        }
    }
    qb = _qb(
        "Do you now, or in the future, require visa sponsorship?",
        ["I will require sponsorship", "I do not require sponsorship"],
    )
    tasks = build_macro_tasks(profile=profile, snapshot_map={}, question_blocks=[qb])
    assert len(tasks) == 1
    assert tasks[0].expected_options == ["I do not require sponsorship"]
    assert tasks[0].mapping_reason == "visa_sponsorship"


def test_general_mapper_handles_remote_preference_enum():
    profile = {"work_preferences": {"remote_work_preference": "hybrid"}}
    qb = _qb(
        "What is your preferred work arrangement?",
        ["Remote", "Hybrid", "Onsite"],
    )
    tasks = build_macro_tasks(profile=profile, snapshot_map={}, question_blocks=[qb])
    assert len(tasks) == 1
    assert tasks[0].expected_options == ["Hybrid"]
    assert tasks[0].mapping_reason == "remote_preference"


def test_general_mapper_handles_office_multi_select_by_location_overlap():
    profile = {
        "work_preferences": {
            "preferred_locations": [
                "San Francisco, California, United States",
                "New York, New York, United States",
            ]
        }
    }
    qb = QuestionBlock(
        question_id="q1",
        question_text="Which offices are you willing to work out of?",
        control_type="choice_group",
        required=True,
        has_error=False,
        options=[
            OptionNode(
                text="New York City (Chelsea)",
                role="checkbox",
                selected=False,
                ref_id="e1",
            ),
            OptionNode(
                text="San Francisco",
                role="checkbox",
                selected=False,
                ref_id="e2",
            ),
            OptionNode(
                text="Remote only",
                role="checkbox",
                selected=False,
                ref_id="e3",
            ),
        ],
        selected_options=[],
    )
    tasks = build_macro_tasks(profile=profile, snapshot_map={}, question_blocks=[qb])
    assert len(tasks) == 1
    assert tasks[0].task_type == "question_multi"
    assert tasks[0].expected_options == ["San Francisco", "New York City (Chelsea)"]
    assert tasks[0].mapping_reason == "preferred_locations"


def test_general_mapper_supports_custom_option_rules_for_abc():
    profile = {
        "option_rules": [
            {
                "question_keywords": ["security clearance"],
                "answer": "B",
            }
        ]
    }
    qb = _qb(
        "What is your security clearance level?",
        ["A", "B", "C"],
    )
    tasks = build_macro_tasks(profile=profile, snapshot_map={}, question_blocks=[qb])
    assert len(tasks) == 1
    assert tasks[0].expected_options == ["B"]
    assert tasks[0].mapping_reason == "custom_rule_1"


def test_general_mapper_skips_question_when_no_reliable_match():
    profile = {"demographics": {"gender": "Male"}}
    qb = _qb("Pick your favorite color", ["Red", "Green", "Blue"])
    tasks = build_macro_tasks(profile=profile, snapshot_map={}, question_blocks=[qb])
    assert tasks == []

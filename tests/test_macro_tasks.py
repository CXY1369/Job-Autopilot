from autojobagent.core.macro_tasks import build_macro_tasks
from autojobagent.core.semantic_tree import OptionNode, QuestionBlock
from autojobagent.core.ui_snapshot import SnapshotItem


def _qb(question: str, options: list[str], role: str = "button") -> QuestionBlock:
    return QuestionBlock(
        question_id="q1",
        question_text=question,
        control_type="single_choice",
        required=True,
        has_error=False,
        options=[
            OptionNode(text=opt, role=role, selected=False, ref_id=f"e{i + 1}")
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
    assert len(tasks) == 1
    assert tasks[0].task_type == "inference_required"
    assert tasks[0].mapping_reason == "inference_required_unmapped"


def test_macro_tasks_include_required_profile_field_fills():
    profile = {
        "personal": {
            "full_name": "Xingyu Chen",
            "email": "cxy1368@gmail.com",
            "linkedin": "https://linkedin.com/in/xingyuchen123/",
        }
    }
    snapshot_map = {
        "e1": SnapshotItem(
            ref="e1",
            role="textbox",
            name="Name",
            nth=0,
            required=True,
            value_hint="",
            input_type="text",
        ),
        "e2": SnapshotItem(
            ref="e2",
            role="textbox",
            name="Email",
            nth=0,
            required=True,
            value_hint="",
            input_type="email",
        ),
        "e3": SnapshotItem(
            ref="e3",
            role="textbox",
            name="LinkedIn Profile",
            nth=0,
            required=True,
            value_hint="",
            input_type="text",
        ),
    }
    tasks = build_macro_tasks(profile=profile, snapshot_map=snapshot_map, question_blocks=[])
    fill_tasks = [t for t in tasks if t.task_type == "field_fill"]
    assert len(fill_tasks) == 3
    assert any(t.field_selector == "Name" and t.target_value == "Xingyu Chen" for t in fill_tasks)
    assert any(t.field_selector == "Email" and t.target_value == "cxy1368@gmail.com" for t in fill_tasks)
    assert any(
        t.field_selector == "LinkedIn Profile"
        and t.target_value == "https://linkedin.com/in/xingyuchen123/"
        for t in fill_tasks
    )


def test_macro_tasks_include_resume_upload_and_optional_motivation_fill():
    profile = {
        "files": {"default_resume": "/tmp/resume.pdf"},
        "common_answers": {"why_this_company": "I am excited about this role."},
    }
    snapshot_map = {
        "e1": SnapshotItem(
            ref="e1",
            role="file_input",
            name="Resume",
            nth=0,
            required=True,
            input_type="file",
        ),
        "e2": SnapshotItem(
            ref="e2",
            role="textbox",
            name="Why are you interested in working here?",
            nth=0,
            required=False,
            value_hint="",
            input_type="text",
        ),
    }
    tasks = build_macro_tasks(profile=profile, snapshot_map=snapshot_map, question_blocks=[])
    assert any(
        t.task_type == "file_upload"
        and t.field_selector == "Resume"
        and t.target_value == "/tmp/resume.pdf"
        for t in tasks
    )
    assert any(
        t.task_type == "field_fill_optional"
        and t.field_selector == "Why are you interested in working here?"
        for t in tasks
    )


def test_macro_tasks_resume_upload_prefers_job_resume_used():
    profile = {"files": {"default_resume": "/tmp/default.pdf"}}
    snapshot_map = {
        "e1": SnapshotItem(
            ref="e1",
            role="file_input",
            name="Resume",
            nth=0,
            required=True,
            input_type="file",
        )
    }
    tasks = build_macro_tasks(
        profile=profile,
        snapshot_map=snapshot_map,
        question_blocks=[],
        preferred_resume_path="/tmp/jd_matched.pdf",
    )
    assert any(
        t.task_type == "file_upload"
        and t.field_selector == "Resume"
        and t.target_value == "/tmp/jd_matched.pdf"
        for t in tasks
    )


def test_macro_tasks_mark_required_non_resume_upload_as_manual_required():
    profile = {}
    snapshot_map = {
        "e1": SnapshotItem(
            ref="e1",
            role="file_input",
            name="Portfolio",
            nth=0,
            required=True,
            input_type="file",
        )
    }
    tasks = build_macro_tasks(profile=profile, snapshot_map=snapshot_map, question_blocks=[])
    assert any(
        t.task_type == "manual_required"
        and t.mapping_reason == "required_non_resume_upload"
        for t in tasks
    )


def test_macro_tasks_keep_unmapped_choice_question_even_when_not_required():
    profile = {}
    qb = QuestionBlock(
        question_id="q1",
        question_text="Have you worked on a data engineering initiative 0-1?",
        control_type="single_choice",
        required=False,
        has_error=False,
        options=[
            OptionNode(text="Yes", role="button", selected=False, ref_id="e1"),
            OptionNode(text="No", role="button", selected=False, ref_id="e2"),
        ],
        selected_options=[],
    )
    tasks = build_macro_tasks(profile=profile, snapshot_map={}, question_blocks=[qb])
    assert len(tasks) == 1
    assert tasks[0].task_type == "inference_required"
    assert tasks[0].mapping_reason == "inference_unmapped_question"


def test_macro_tasks_map_experience_prompt_to_generated_summary():
    profile = {"experience": {"current_title": "Machine Learning Engineer"}}
    snapshot_map = {
        "e1": SnapshotItem(
            ref="e1",
            role="textbox",
            name="In a couple sentences, describe your work experience that relates to analytics engineering:",
            nth=0,
            required=True,
            value_hint="",
            input_type="text",
        )
    }
    tasks = build_macro_tasks(profile=profile, snapshot_map=snapshot_map, question_blocks=[])
    assert len(tasks) == 1
    assert tasks[0].task_type == "field_fill"
    assert tasks[0].mapping_reason == "common_answers.experience_summary"
    assert "experience" in (tasks[0].target_value or "").lower()

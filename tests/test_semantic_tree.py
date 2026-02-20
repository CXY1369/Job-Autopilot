from autojobagent.core.semantic_tree import (
    build_form_graph,
    build_question_blocks,
    format_form_graph,
    format_question_blocks,
)
from autojobagent.core.ui_snapshot import SnapshotItem


class _TreePage:
    def evaluate(self, _script: str):
        return [
            {
                "question_id": "q1",
                "question_text": "Are you legally authorized to work in the United States?",
                "control_type": "single_choice",
                "required": True,
                "has_error": False,
                "options": [
                    {"text": "Yes", "role": "button", "selected": True},
                    {"text": "No", "role": "button", "selected": False},
                ],
            },
            {
                "question_id": "q2",
                "question_text": "Do you require visa sponsorship?",
                "control_type": "single_choice",
                "required": True,
                "has_error": True,
                "options": [
                    {"text": "Yes", "role": "button", "selected": False},
                    {"text": "No", "role": "button", "selected": True},
                ],
            },
        ]


def test_build_question_blocks_extracts_generalized_options():
    snapshot_map = {
        "e6": SnapshotItem(ref="e6", role="button", name="Yes", nth=0),
        "e7": SnapshotItem(ref="e7", role="button", name="No", nth=0),
        "e8": SnapshotItem(ref="e8", role="button", name="Yes", nth=1),
        "e9": SnapshotItem(ref="e9", role="button", name="No", nth=1),
    }
    blocks = build_question_blocks(_TreePage(), snapshot_map)
    assert len(blocks) == 2
    assert blocks[0].question_text.startswith("Are you legally authorized")
    assert len(blocks[0].options) == 2
    assert blocks[0].selected_options == ["Yes"]
    assert blocks[1].has_error is True


def test_format_question_blocks_human_readable():
    snapshot_map = {
        "e1": SnapshotItem(ref="e1", role="button", name="A", nth=0),
        "e2": SnapshotItem(ref="e2", role="button", name="B", nth=0),
    }

    class _Page:
        def evaluate(self, _script: str):
            return [
                {
                    "question_id": "q1",
                    "question_text": "Pick one option",
                    "control_type": "single_choice",
                    "required": False,
                    "has_error": False,
                    "options": [
                        {"text": "A", "role": "button", "selected": False},
                        {"text": "B", "role": "button", "selected": True},
                    ],
                }
            ]

    blocks = build_question_blocks(_Page(), snapshot_map)
    rendered = format_question_blocks(blocks)
    assert "Pick one option" in rendered
    assert "options:" in rendered


def test_build_form_graph_extracts_required_and_submit():
    snapshot_map = {
        "e1": SnapshotItem(
            ref="e1", role="textbox", name="Email", nth=0, required=True, value_hint=""
        ),
        "e2": SnapshotItem(
            ref="e2",
            role="textbox",
            name="Name",
            nth=0,
            required=True,
            value_hint="Xingyu",
        ),
        "e3": SnapshotItem(ref="e3", role="button", name="Submit Application", nth=0),
    }
    blocks = build_question_blocks(_TreePage(), snapshot_map)
    graph = build_form_graph(
        current_url="https://jobs.ashbyhq.com/suno/role/application",
        snapshot_map=snapshot_map,
        question_blocks=blocks,
        error_snippets=["Please complete this required field."],
    )
    assert graph.page_scope.startswith("jobs.ashbyhq.com|")
    assert len(graph.required_unfilled) == 1
    assert graph.submit_refs == ["e3"]
    rendered = format_form_graph(graph)
    assert "required_unfilled=1" in rendered
    assert "submit_candidates=1" in rendered

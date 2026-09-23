"""Author promotion is a build stage, not a one-off script run.

`save_entities` writes `trust` with ON DUPLICATE KEY UPDATE, so the seed stage
resets every entity to the level the seeder computes. A promotion applied out of
band survives exactly until the next `build_knowledge` and then disappears --
measured: 258 `author_attested` people silently reverted to `provisional`, and
501 AUTHORED claims lost their projectable subject.

`pi-promotion` already solved this by being a stage. These tests pin that
`author-promotion` is one too, and that it runs after seeding.
"""
from __future__ import annotations

import inspect

import scripts.build_knowledge as bk


def test_the_stage_exists():
    assert hasattr(bk.Build, "author_promotion")


def test_it_runs_in_the_pipeline():
    assert "self.author_promotion()" in inspect.getsource(bk.Build.run)


def test_it_runs_after_seeding():
    """Before seeding, the promotion would be overwritten by it."""
    source = inspect.getsource(bk.Build.run)
    assert source.index("self.seed()") < source.index("self.author_promotion()")


def test_it_runs_after_ambiguity_marking():
    """`evaluate_promotions` refuses a name marked ambiguous, so the marks have
    to exist before it decides."""
    source = inspect.getsource(bk.Build.run)
    assert source.index("self.ambiguity()") < source.index("self.author_promotion()")


def test_it_runs_before_the_index_is_refreshed():
    """Everything downstream resolves against the promoted store, so the
    promotion must land before the index it is read through."""
    source = inspect.getsource(bk.Build.run)
    assert source.index("self.author_promotion()") < source.index(
        "self.index(refresh=True)")


def test_it_uses_the_author_module_not_the_pi_one():
    source = inspect.getsource(bk.Build.author_promotion)
    assert "app.knowledge.author_promotion" in source


def test_the_pi_stage_is_untouched():
    """The two are separate promotions with separate grants."""
    source = inspect.getsource(bk.Build.promotion)
    assert "app.knowledge.pi_promotion" in source
    assert "author_promotion" not in source


def test_it_writes_nothing_on_a_dry_run():
    source = inspect.getsource(bk.Build.author_promotion)
    assert "if self.writes:" in source
    decide = source.index("evaluate_promotions()")
    apply_at = source.index("apply_promotions(decisions)")
    guard = source.index("if self.writes:")
    assert decide < guard < apply_at, "applying must sit behind the write guard"


def test_it_honours_the_skip_flag():
    assert "skip=self.o.skip_promotion" in inspect.getsource(bk.Build.author_promotion)

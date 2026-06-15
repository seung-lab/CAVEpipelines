"""The per-chunk done marker: a 1-byte presence flag, written only while the claim holds."""

from cave_pipeline.distribution import lock


class _Fake:
    """Minimal kvdbclient stand-in recording writes; the claim is held by default."""

    def __init__(self, done=None, held=True):
        self.done = done  # what _read_byte_row returns for the _DONE column
        self.held = held
        self.written = []

    def renew_lock_by_row_key(self, row_key, op):
        return self.held

    def unlock_by_row_key(self, row_key, op):
        pass

    def lock_by_row_key(self, row_key, op):
        return True

    def mutate_row(self, row_key, values):
        return (row_key, dict(values))

    def write(self, mutations):
        self.written.extend(mutations)

    def _read_byte_row(self, row_key, columns=None):
        return self.done or []


def test_mark_done_value_is_token_independent():
    # the marker records "done", not the claim token — same value for any token
    a, b = _Fake(), _Fake()
    lock.mark_done(a, 5, token=1)
    lock.mark_done(b, 5, token=999999)
    [(_, va)], [(_, vb)] = a.written, b.written
    assert va == vb == {lock._DONE: lock._MARK}


def test_done_marker_serializes_to_one_byte():
    assert len(lock._DONE.serialize(lock._MARK)) == 1


def test_is_done_is_presence_only():
    assert lock._is_done(_Fake(done=[lock._MARK]), b"k") is True
    assert lock._is_done(_Fake(done=[]), b"k") is False


def test_mark_done_skips_write_when_fenced_out():
    fenced = _Fake(held=False)
    assert lock.mark_done(fenced, 5, token=1) is False
    assert fenced.written == []

"""Conservative spelling comparison for previously observed domains."""


def one_edit_apart(a: str, b: str) -> bool:
    # Keep the suffix fixed; never infer another country or top-level domain.
    if a.rsplit(".", 1)[-1] != b.rsplit(".", 1)[-1] or abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) == 1
    if len(a) > len(b):
        a, b = b, a
    return any(a == b[:i] + b[i + 1:] for i in range(len(b)))

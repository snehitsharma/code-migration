def helper(n):
    return n * 2


def is_even(n):
    return True if n == 0 else is_odd(n - 1)


def is_odd(n):
    return False if n == 0 else is_even(n - 1)


def double_all(values):
    def inner(v):
        return helper(v)

    return [inner(v) for v in values]

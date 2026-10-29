"""Collatz helpers for the Collatz-Mini project."""


def step(n: int) -> int:
    """One Collatz step: n/2 for even n, 3n + 1 for odd n."""
    return n // 2 if n % 2 == 0 else 3 * n + 1


def steps(n: int) -> int:
    """Number of steps from n to the first 1 (s(1) = 0)."""
    count = 0
    while n != 1:
        n = step(n)
        count += 1
    return count


if __name__ == "__main__":
    for k in range(6):
        print(2**k, steps(2**k))

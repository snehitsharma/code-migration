import mathops
from utils import format_name, helper as clean


def main():
    print(format_name("Ada", "Lovelace"))
    print(clean(" x "))
    return mathops.is_even(4) and mathops.double_all([1, 2])

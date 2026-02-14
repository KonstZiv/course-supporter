# Python Basics Tutorial: Variables, Functions, and Loops

This tutorial covers the three foundational concepts every Python programmer
needs to know. We'll work through practical examples that you can type along
with in your Python interpreter.

## Variables and Data Types

### Creating Variables

In Python, you create a variable by assigning a value to a name using the
`=` operator. Python is dynamically typed, so you don't need to specify the
type — Python infers it from the value.

```python
name = "Alice"
age = 30
price = 19.99
is_student = True
```

### Built-in Data Types

Python's most commonly used data types are:

- **str** — text enclosed in quotes: `"hello"` or `'hello'`
- **int** — whole numbers: `42`, `-7`, `0`
- **float** — decimal numbers: `3.14`, `0.001`
- **bool** — logical values: `True` or `False`
- **NoneType** — the special value `None`, meaning "no value"

### Type Conversion

You can convert between types using built-in functions:

```python
number = int("42")       # string to int
text = str(100)          # int to string
decimal = float("3.14")  # string to float
```

Use `type()` to check a variable's type:

```python
print(type(name))   # <class 'str'>
print(type(age))    # <class 'int'>
print(type(price))  # <class 'float'>
```

## Functions

### Defining a Function

A function is a named block of reusable code. Use the `def` keyword to define
one:

```python
def greet(name):
    """Return a greeting message."""
    return f"Hello, {name}!"
```

Call it by passing an argument:

```python
message = greet("Bob")
print(message)  # Hello, Bob!
```

### Parameters and Default Values

Functions can accept multiple parameters and have default values:

```python
def power(base, exponent=2):
    """Raise base to exponent (default: squared)."""
    return base ** exponent

print(power(3))      # 9 (3 squared)
print(power(2, 10))  # 1024 (2 to the 10th)
```

### Returning Values

The `return` statement sends a result back to the caller. Without it, the
function returns `None`.

```python
def divide(a, b):
    """Divide a by b, returning quotient and remainder."""
    if b == 0:
        return None
    return a // b, a % b

result = divide(17, 5)  # returns (3, 2)
```

### Practical Example: Temperature Converter

```python
def celsius_to_fahrenheit(celsius):
    """Convert Celsius temperature to Fahrenheit."""
    return celsius * 9 / 5 + 32

def fahrenheit_to_celsius(fahrenheit):
    """Convert Fahrenheit temperature to Celsius."""
    return (fahrenheit - 32) * 5 / 9

print(celsius_to_fahrenheit(100))  # 212.0
print(fahrenheit_to_celsius(32))   # 0.0
```

## Loops

### For Loops

A `for` loop iterates over a sequence — a list, string, range, or other
iterable:

```python
fruits = ["apple", "banana", "cherry"]
for fruit in fruits:
    print(fruit)
```

Use `range()` to loop a specific number of times:

```python
for i in range(5):
    print(i)  # prints 0, 1, 2, 3, 4

for i in range(2, 8, 2):
    print(i)  # prints 2, 4, 6
```

### While Loops

A `while` loop repeats as long as its condition is `True`:

```python
count = 0
while count < 5:
    print(count)
    count += 1
```

Always make sure the condition will eventually become `False` — otherwise
you create an infinite loop.

### Break and Continue

Use `break` to exit a loop early and `continue` to skip to the next iteration:

```python
for number in range(10):
    if number == 7:
        break  # stop at 7
    if number % 2 == 0:
        continue  # skip even numbers
    print(number)  # prints 1, 3, 5
```

### Practical Example: Finding Prime Numbers

```python
def is_prime(n):
    """Check if a number is prime."""
    if n < 2:
        return False
    for i in range(2, int(n ** 0.5) + 1):
        if n % i == 0:
            return False
    return True

primes = []
for num in range(2, 50):
    if is_prime(num):
        primes.append(num)

print(primes)  # [2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37, 41, 43, 47]
```

## Putting It All Together

Here is a complete example that combines variables, functions, and loops to
analyze a list of student grades:

```python
def analyze_grades(grades):
    """Analyze a list of grades and return statistics."""
    if not grades:
        return None

    total = 0
    highest = grades[0]
    lowest = grades[0]
    passing = 0

    for grade in grades:
        total += grade
        if grade > highest:
            highest = grade
        if grade < lowest:
            lowest = grade
        if grade >= 60:
            passing += 1

    average = total / len(grades)
    pass_rate = passing / len(grades) * 100

    return {
        "average": round(average, 1),
        "highest": highest,
        "lowest": lowest,
        "pass_rate": f"{pass_rate:.0f}%",
    }

student_grades = [85, 92, 78, 45, 67, 91, 53, 88, 76, 95]
result = analyze_grades(student_grades)
print(result)
```

This example demonstrates how variables track state, functions organize logic,
and loops process collections — the core patterns you'll use in every Python
program.

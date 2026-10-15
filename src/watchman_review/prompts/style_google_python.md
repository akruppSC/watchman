# Style conventions: Google Python Style Guide (summary for review)

The reference is the Google Python Style Guide, https://google.github.io/styleguide/pyguide.html.
Cite the section number when a finding is about one of these. Do not report formatting
(line length, indentation, whitespace, blank lines): a formatter owns that. Report a departure only
when the *changed* code introduces it and it makes the code harder for a senior developer to read.
The repository's own conventions, if given below, win over this summary.

## What a linter cannot judge (your job)

- **§3.18 Function length.** Prefer small, focused functions. About 40 lines is the point at which to
  ask whether it can be split without harming the program's structure. Flag a long function only
  when there is a natural seam (separate responsibilities, a branch ladder, repeated blocks).
- **§2.4 Exceptions.** Use built-in exceptions where they fit; never a catch-all `except:` or
  `except Exception:` unless re-raising or isolating a failure at an outermost boundary. Keep `try`
  bodies minimal: the more code inside, the more likely an unexpected line hides a real error. Do not
  use `assert` to validate arguments or drive logic; raise a real exception.
- **§2.19 Power features.** Avoid metaclasses, dynamic attribute games, `getattr`/reflection tricks,
  import hacks, and clever code where straightforward code is longer but obvious.
- **§2.13 Properties.** Only for cheap, unsurprising, trivially derived values. A property that merely
  gets and sets a plain attribute should be a public attribute.
- **§2.7, §2.10, §2.11 Comprehensions, lambdas, conditional expressions.** Fine when simple. A
  comprehension with several `for` clauses or filters, a multi-line lambda, or a ternary whose parts
  don't each fit on one line should be a normal loop, function, or `if` statement.
- **§2.17 Decorators.** Use judiciously; no `@staticmethod` (write a module-level function);
  `@classmethod` only for named constructors.
- **§3.8 Docstrings and comments.** Every public module, class and function has a docstring that lets
  a caller use it without reading the code: a one-line summary, then `Args:`, `Returns:` (or
  `Yields:`) and `Raises:` when the signature is not self-explanatory. Comments explain *why*, not
  what, and don't restate the code.
- **§3.16 Naming.** Names say what a thing is or does. `lower_with_under` for functions, methods,
  variables and modules; `CapWords` for classes; `CAPS_WITH_UNDER` for constants. Avoid single
  letters (except conventional loop counters, `e` for exceptions, `f` for files), and avoid names that
  hide the type or role (`data`, `info`, `tmp`, `do_stuff`).
- **§2.5 Mutable global state.** Avoid it. Module constants are fine; a mutable module-level
  object must be internal (`_name`) and justified in a comment.
- **§2.12, §2.14 Defaults and truthiness.** No mutable default arguments; use `if x is None:` for None
  checks and implicit falsiness (`if not items:`) instead of `len(items) == 0`.
- **§2.21 Type annotations.** New or changed public APIs have type annotations that match what the
  code actually does.

## Project-level readability (also your job)

- The change fits the structure that is already there: it reuses existing helpers instead of
  copying them, puts code in the module where a reader would look for it, and doesn't add a
  parallel mechanism for something the project already does.
- A reader can follow the main flow without keeping many things in their head at once: no deep
  nesting, no function that does validation, computation, formatting and I/O together.
- Tests accompany behavior changes and say what they check.

Flag only what you can point to in the diff, citing the section and the lines.

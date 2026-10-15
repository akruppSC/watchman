import json, sys, pathlib
here = pathlib.Path(__file__).parent
args = sys.argv[1:]
with open(here / "calls.log", "a") as fh:
    fh.write(json.dumps(args) + "\n")
if args[:2] == ["live", "start"]:
    sys.exit(0)
kind = args[args.index("--kind") + 1]
table = json.loads((here / "feedback.json").read_text())
entry = table.get(kind)
if entry == "FAIL":
    sys.stderr.write("boom")
    sys.exit(1)
if entry == "GARBAGE":
    print("not json")
    sys.exit(0)
print(json.dumps(entry))

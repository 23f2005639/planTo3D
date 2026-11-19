"""The detector on the made-up sample drawing (examples/sample_plan.pdf): every wall, door, window and room must be
found, and the scale read from the room sizes.  python3 tests/test_sample.py"""
import os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.dirname(HERE), os.path.join(os.path.dirname(HERE), "examples")]
import detect  # noqa: E402
import make_sample as M  # noqa: E402

plan = detect.detect(os.path.join(HERE, "..", "examples", "sample_plan.pdf"))
assert plan["scale_source"] == "labels" and abs(plan["scale_pt_per_ft"] - M.S) < 0.02 * M.S, plan["scale_pt_per_ft"]
assert len(plan["floors"]) == 1, [f["name"] for f in plan["floors"]]
f = plan["floors"][0]
assert f["name"] == "Ground Floor" and all(abs(a - b) < 0.1 for a, b in zip(f["size"], M.EXPECT["size"])), (f["name"], f["size"])
kinds = [o["type"] for o in f["openings"]]
assert (kinds.count("door"), kinds.count("window")) == (M.EXPECT["doors"], M.EXPECT["windows"]), kinds
assert sorted(r["type"] for r in f["rooms"]) == M.EXPECT["rooms"], [r["type"] for r in f["rooms"]]
# every drawn opening is where the drawing puts it (to a tenth of a foot)
drawn = [(k, *((c0, c1, y0, y1) if x1 - x0 > y1 - y0 else (x0, x1, c0, c1)))
         for (x0, x1, y0, y1), cuts in M.WALLS for k, c0, c1, _ in cuts]
for k, *r in drawn:
    assert any(o["type"] == k and all(abs(a - b) < 0.1 for a, b in zip(o["rect"], r)) for o in f["openings"]), (k, r)
print("ok   sample plan: %d walls, %d doors, %d windows, %d rooms, scale %.1f pt/ft" % (
    len(f["walls"]), kinds.count("door"), kinds.count("window"), len(f["rooms"]), plan["scale_pt_per_ft"]))

# more pages: a sheet of slanted lines (a rotated site plan) is skipped, and a page drawn at another scale keeps it
import subprocess, tempfile  # noqa: E402
with tempfile.TemporaryDirectory() as d:
    pages = [os.path.join(HERE, "..", "examples", "sample_plan.pdf")]
    for k, stream in enumerate((b"".join(b"%d 10 m %d 400 l S\n" % (10 + 2 * i, 60 + 2 * i) for i in range(200)),
                                b"q 0.75 0 0 0.75 0 0 cm\n" + M.stream + b"\nQ")):
        pages.append(os.path.join(d, f"p{k}.pdf"))
        open(pages[-1], "wb").write(M.pdf_page(stream))
    subprocess.run(["pdfunite", *pages, os.path.join(d, "all.pdf")], check=True)
    many = detect.detect(os.path.join(d, "all.pdf"))
fs = many["floors"]
assert [f["page"] for f in fs] == [1, 3], [f["page"] for f in fs]
assert abs(fs[0]["scale_pt_per_ft"] - M.S) < 0.02 * M.S and abs(fs[1]["scale_pt_per_ft"] - 0.75 * M.S) < 0.02 * M.S, fs
assert all(abs(a - b) < 0.1 for f in fs for a, b in zip(f["size"], M.EXPECT["size"])), [f["size"] for f in fs]
print("ok   more pages: a sheet without walls skipped, each page read at its own scale")

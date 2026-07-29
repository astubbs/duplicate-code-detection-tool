#!/usr/bin/env python

import os
import sys
import io
import json
import contextlib
import requests
import argparse

import duplicate_code_detection

WARNING_SUFFIX = " ⚠️"


def make_markdown_table(array):
    """Input: Python list with rows of table as lists
               First element as header.
        Output: String to put into a .md file

    Ex Input:
        [["Name", "Age", "Height"],
         ["Jake", 20, 5'10],
         ["Mary", 21, 5'7]]

     Adopted from: https://gist.github.com/m0neysha/219bad4b02d2008e0154
    """
    markdown = "\n" + str("| ")

    for e in array[0]:
        to_add = " " + str(e) + str(" |")
        markdown += to_add
    markdown += "\n"

    markdown += "|"
    for i in range(len(array[0])):
        markdown += str("-------------- | ")
    markdown += "\n"

    markdown_characters = 0
    max_characters = 65000
    for entry in array[1:]:
        markdown += str("| ")
        for e in entry:
            to_add = str(e) + str(" | ")
            markdown += to_add
        markdown += "\n"
        markdown_characters += len(markdown)
        if markdown_characters > max_characters:
            markdown += "\n" + WARNING_SUFFIX + " "
            markdown += "Results were omitted because the report was too large. "
            markdown += "Please consider ignoring results below a certain threshold.\n"
            break

    return markdown + "\n"


def get_markdown_link(file, url):
    return "[%s](%s%s)" % (file, url, file)


def get_warning(similarity, warn_threshold):
    return (
        str(similarity)
        if similarity < int(warn_threshold)
        else str(similarity) + WARNING_SUFFIX
    )


def similarities_to_markdown(similarities, url_prefix, warn_threshold):
    markdown = str()
    for checked_file in similarities.keys():
        markdown += "<details><summary>%s</summary>\n\n" % checked_file
        markdown += "### 📄 %s\n" % get_markdown_link(checked_file, url_prefix)

        table_header = ["File", "Similarity (%)"]
        table_contents = [
            [get_markdown_link(f, url_prefix), get_warning(s, warn_threshold)]
            for (f, s) in similarities[checked_file].items()
        ]
        # Sort table contents based on similarity
        table_contents.sort(
            reverse=True, key=lambda row: float(row[1].replace(WARNING_SUFFIX, ""))
        )
        entire_table = [[] for _ in range(len(table_contents) + 1)]
        entire_table[0] = table_header
        for i in range(1, len(table_contents) + 1):
            entire_table[i] = table_contents[i - 1]

        markdown += make_markdown_table(entire_table)
        markdown += "</details>\n"

    return markdown


def split_and_trim(input_list):
    return [token.strip() for token in input_list.split(",")]


def to_absolute_path(paths):
    return [os.path.abspath(path) for path in paths]


def compute_delta(pr_similarity, base_similarity):
    """Compare PR results against base and find new/changed similarities.

    The engine emits a symmetric matrix (both ``[A][B]`` and ``[B][A]``), so each
    file pair is deduplicated to a single unordered entry to avoid double-counting
    it in the report.
    """
    new_pairs = []
    increased_pairs = []
    seen = set()
    for file_a in pr_similarity:
        for file_b in pr_similarity[file_a]:
            pr_val = pr_similarity[file_a][file_b]
            if not isinstance(pr_val, (int, float)):
                continue
            pair_key = tuple(sorted((file_a, file_b)))
            if pair_key in seen:
                continue
            seen.add(pair_key)
            base_val = 0
            if file_a in base_similarity and file_b in base_similarity[file_a]:
                bv = base_similarity[file_a][file_b]
                base_val = bv if isinstance(bv, (int, float)) else 0
            if base_val == 0 and pr_val > 0:
                new_pairs.append((file_a, file_b, pr_val))
            elif pr_val > base_val:
                increased_pairs.append((file_a, file_b, base_val, pr_val))
    return new_pairs, increased_pairs


def delta_to_markdown(new_pairs, increased_pairs, max_increase, fail_above):
    """Generate markdown for the comparison section and decide pass/fail.

    A PR fails when it INTRODUCES duplication, i.e.:
      - a brand-new file pair whose similarity exceeds ``fail_above`` (newly
        introduced duplication - e.g. a copy-pasted pair of files), or
      - an existing pair whose similarity increased by more than ``max_increase``.
    Pre-existing similarity that the PR did not change never fails.

    Known limitation (path-keyed, no content tracking): renaming/moving one file of
    a pre-existing similar pair makes it look like a brand-new pair, so it is judged
    against ``fail_above`` rather than being recognised as pre-existing. And because
    the gate is a per-pair delta, similarity can still creep up across many PRs that
    each stay under ``max_increase``.
    """
    md = ""
    failed = False

    if new_pairs:
        md += "\n### :new: New file similarities introduced\n\n"
        md += "| File A | File B | Similarity (%) |\n"
        md += "|--------|--------|--:|\n"
        for fa, fb, val in sorted(new_pairs, key=lambda x: -x[2])[:20]:
            status = " :x:" if val > fail_above else ""
            md += "| %s | %s | %.1f%s |\n" % (fa, fb, val, status)
            if val > fail_above:
                failed = True
        if len(new_pairs) > 20:
            md += "\n...and %d more\n" % (len(new_pairs) - 20)

    if increased_pairs:
        md += "\n### :small_red_triangle: Increased similarities\n\n"
        md += "| File A | File B | Base (%) | PR (%) | Change |\n"
        md += "|--------|--------|--:|--:|--:|\n"
        for fa, fb, bv, pv in sorted(increased_pairs, key=lambda x: -(x[3] - x[2]))[:20]:
            # Round to the inputs' precision so binary-float subtraction noise can't
            # push an increase that equals max_increase over the strict-> boundary.
            change = round(pv - bv, 2)
            status = " :x:" if change > max_increase else ""
            md += "| %s | %s | %.1f | %.1f | +%.1f%s |\n" % (fa, fb, bv, pv, change, status)
            if change > max_increase:
                failed = True
        if len(increased_pairs) > 20:
            md += "\n...and %d more\n" % (len(increased_pairs) - 20)

    if not new_pairs and not increased_pairs:
        md += "\n:white_check_mark: No new or increased file similarities introduced by this PR.\n"

    return md, failed


def main():
    parser = argparse.ArgumentParser(
        description="Duplicate code detection action runner"
    )
    parser.add_argument(
        "--latest-head",
        type=str,
        default="master",
        help="The latest commit hash or branch",
    )
    parser.add_argument(
        "--pull-request-id", type=str, required=True, help="The pull request id"
    )
    parser.add_argument(
        "--json-only",
        action="store_true",
        help="Only output JSON results (for base branch scanning)",
    )
    parser.add_argument(
        "--base-results",
        type=str,
        default="",
        help="Path to base branch JSON results for comparison",
    )
    args = parser.parse_args()

    fail_threshold = os.environ.get("INPUT_FAIL_ABOVE")
    directories = os.environ.get("INPUT_DIRECTORIES")
    ignore_directories = os.environ.get("INPUT_IGNORE_DIRECTORIES")
    project_root_dir = os.environ.get("INPUT_PROJECT_ROOT_DIR")
    file_extensions = os.environ.get("INPUT_FILE_EXTENSIONS")
    ignore_threshold = os.environ.get("INPUT_IGNORE_BELOW")
    only_code = os.environ.get("INPUT_ONLY_CODE")

    directories_list = split_and_trim(directories)
    directories_list = to_absolute_path(directories_list)
    ignore_directories_list = (
        split_and_trim(ignore_directories) if ignore_directories != "" else list()
    )
    ignore_directories_list = to_absolute_path(ignore_directories_list)
    file_extensions_list = split_and_trim(file_extensions)
    project_root_dir = os.path.abspath(project_root_dir)

    files_list = None
    ignore_files_list = None
    # Suppress the engine's text output only when --json-only (where we must
    # have clean stdout). In normal mode we want logs in the CI output.
    json_output = args.json_only
    csv_output_path = ""  # No CSV output by default for now in GitHub Actions
    show_loc = False

    # In --json-only mode we must emit ONLY clean JSON on stdout, so capture the
    # engine's own console output. contextlib.redirect_stdout restores stdout even
    # if run() raises; a manual __enter__/__exit__ would leak the redirect and
    # corrupt stdout for every later in-process call (e.g. the test suite).
    stdout_capture = io.StringIO()
    capture = (
        contextlib.redirect_stdout(stdout_capture)
        if args.json_only
        else contextlib.nullcontext()
    )
    with capture:
        detection_result, code_similarity = duplicate_code_detection.run(
            int(fail_threshold),
            directories_list,
            files_list,
            ignore_directories_list,
            ignore_files_list,
            json_output,
            project_root_dir,
            file_extensions_list,
            int(ignore_threshold),
            bool(only_code),
            csv_output_path,
            show_loc,
        )

    if detection_result == duplicate_code_detection.ReturnCode.BAD_INPUT:
        print("Action aborted due to bad user input")
        return detection_result.value

    # --json-only is a data dump used to capture the BASE branch's similarity for
    # later comparison. A successful scan must exit 0 even when the base already
    # exceeds fail_above (which is exactly when comparison matters) - otherwise
    # entrypoint.sh reads the non-zero exit as "base scan failed", throws the base
    # data away, and the PR gets gated on absolute similarity. A genuine BAD_INPUT
    # has already returned non-zero above.
    if args.json_only:
        print(json.dumps(code_similarity))
        return duplicate_code_detection.ReturnCode.SUCCESS.value

    repo = os.environ.get("GITHUB_REPOSITORY")
    files_url_prefix = "https://github.com/%s/blob/%s/" % (repo, args.latest_head)
    warn_threshold = os.environ.get("INPUT_WARN_ABOVE")

    header_message_start = os.environ.get("INPUT_HEADER_MESSAGE_START") + "\n"
    message = header_message_start
    message += "The [tool](https://github.com/platisd/duplicate-code-detection-tool)"
    message += " analyzed your source code and found the following degree of"
    message += " similarity between the files:\n"

    # Add base-vs-PR comparison if base results are available
    if args.base_results and os.path.exists(args.base_results):
        base_similarity = None
        try:
            with open(args.base_results, "r") as f:
                base_similarity = json.load(f)
        except (OSError, ValueError) as err:
            # A corrupt/truncated base file must not crash the whole action - the
            # base scan itself was made fault-tolerant, so match that: degrade to
            # absolute-threshold gating (the pre-comparison verdict) with a warning.
            print(
                "Warning: could not read base results '%s' (%s); "
                "falling back to absolute-threshold gating" % (args.base_results, err)
            )
        if base_similarity is not None:
            max_increase = float(os.environ.get("INPUT_MAX_INCREASE", 100))
            new_pairs, increased_pairs = compute_delta(code_similarity, base_similarity)
            delta_md, delta_failed = delta_to_markdown(
                new_pairs, increased_pairs, max_increase, int(fail_threshold)
            )
            message += delta_md
            # In comparison mode the DELTA is the gate, not the absolute pre-existing
            # similarity. Overwrite (not just elevate) the verdict: a PR that adds no
            # new/increased duplication passes even if the codebase already sits above
            # fail_above; a PR that introduces a new pair above fail_above, or pushes an
            # existing pair past max_increase, fails.
            detection_result = (
                duplicate_code_detection.ReturnCode.THRESHOLD_EXCEEDED
                if delta_failed
                else duplicate_code_detection.ReturnCode.SUCCESS
            )

    # Announce failure based on the FINAL verdict (after any comparison override),
    # so the CI log never contradicts the exit code.
    if detection_result == duplicate_code_detection.ReturnCode.THRESHOLD_EXCEEDED:
        print(
            "Action failed due to maximum similarity threshold exceeded, check the report"
        )

    message += "\n<details><summary>Full similarity report</summary>\n\n"
    message += similarities_to_markdown(
        code_similarity, files_url_prefix, warn_threshold
    )
    message += "\n</details>\n"

    github_token = os.environ.get("INPUT_GITHUB_TOKEN")
    github_api_url = os.environ.get("GITHUB_API_URL")

    request_url = "%s/repos/%s/issues/%s/comments" % (
        github_api_url,
        repo,
        args.pull_request_id,
    )

    headers = {
        "Authorization": "token %s" % github_token,
    }
    report = {"body": message}

    update_existing_comment = os.environ.get("INPUT_ONE_COMMENT", "false").lower() in (
        "true",
        "1",
    )
    comment_updated = False
    if update_existing_comment:
        # If the bot has posted many comments, update the last one
        pr_comments = requests.get(request_url, headers=headers).json()
        for pr_comment in pr_comments[::-1]:
            if pr_comment["body"].startswith(header_message_start):
                update_result = requests.patch(
                    pr_comment["url"],
                    json=report,
                    headers=headers,
                )
                if update_result.status_code != 200:
                    print(
                        "Updating existing comment failed with code: "
                        + str(update_result.status_code)
                    )
                    print(update_result.text)
                    print("Attempting to post a new comment instead")
                else:
                    comment_updated = True
                break

    if not comment_updated:
        post_result = requests.post(
            request_url,
            json=report,
            headers=headers,
        )

        if post_result.status_code != 201:
            print(
                "Posting results to GitHub failed with code: "
                + str(post_result.status_code)
            )
            print(post_result.text)

    with open("message.md", "w") as f:
        f.write(message)

    return detection_result.value


if __name__ == "__main__":
    sys.exit(main())

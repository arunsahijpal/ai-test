import os
import sys
from typing import List, Dict, Optional
from openai import OpenAI
from github import Github
import base64
import json
import logging
import re
from fnmatch import fnmatch
from dataclasses import dataclass


def parse_ai_response(raw_response: str):
    """
    Safely parses OpenAI response to JSON, fixing common issues like unescaped backslashes.
    """
    import re

    try:
        # Fix common invalid escapes like \D or \M
        corrected = re.sub(r'\\(?![\\/"bfnrtu])', r'\\\\', raw_response)

        return json.loads(corrected)
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse OpenAI's response as JSON: {e}")
        logger.debug(f"Raw response for debugging:\n{raw_response}")
        return []


# Set up logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

@dataclass
class FileFilterConfig:
    whitelist_patterns: List[str]
    blacklist_patterns: List[str]

    @classmethod
    def from_env(cls) -> 'FileFilterConfig':
        """Create config from environment variables."""
        whitelist = os.getenv('PR_REVIEW_WHITELIST', '').split(',')
        blacklist = os.getenv('PR_REVIEW_BLACKLIST', '').split(',')

        # Clean up empty strings and whitespace
        whitelist = [p.strip() for p in whitelist if p.strip()]
        blacklist = [p.strip() for p in blacklist if p.strip()]

        # If no whitelist is specified, default to allowing all files
        if not whitelist:
            whitelist = ['*']

        return cls(whitelist_patterns=whitelist, blacklist_patterns=blacklist)

    def should_review_file(self, filename: str) -> bool:
        """
        Determine if a file should be reviewed based on whitelist and blacklist patterns.
        Blacklist takes precedence over whitelist.
        """
        # First check blacklist - if file matches any blacklist pattern, exclude it
        for pattern in self.blacklist_patterns:
            if fnmatch(filename, pattern):
                logger.debug(f"File {filename} matched blacklist pattern {pattern}")
                return False

        # Then check whitelist - file must match at least one whitelist pattern
        for pattern in self.whitelist_patterns:
            if fnmatch(filename, pattern):
                logger.debug(f"File {filename} matched whitelist pattern {pattern}")
                return True

        logger.debug(f"File {filename} did not match any whitelist patterns")
        return False

class PRReviewer:
    def __init__(self):
        self.github_token = os.environ["GITHUB_TOKEN"]
        self.openai_key = os.environ["OPENAI_API_KEY"]
        self.event_path = os.environ["GITHUB_EVENT_PATH"]
        self.repository = os.environ["GITHUB_REPOSITORY"]

        # Initialize file filter config
        self.file_filter = FileFilterConfig.from_env()
        logger.info(f"Initialized with whitelist: {self.file_filter.whitelist_patterns}")
        logger.info(f"Initialized with blacklist: {self.file_filter.blacklist_patterns}")

        # Initialize API clients
        self.openai_client = OpenAI(api_key=self.openai_key)
        self.github = Github(self.github_token)

        # Load PR event data
        try:
            with open(self.event_path, 'r') as f:
                self.event_data = json.load(f)
            self.pr_number = self.event_data["number"]
            logger.info(f"Initialized PR reviewer for PR #{self.pr_number}")

            # Get repository and PR objects
            self.repo = self.github.get_repo(self.repository)
            self.pull_request = self.repo.get_pull(self.pr_number)

        except Exception as e:
            logger.error(f"Error initializing: {e}")
            raise

    def get_existing_comments(self):
        """Get all existing review comments on the PR."""
        comments = self.pull_request.get_review_comments()
        existing = {}
        for comment in comments:
            key = f"{comment.path}:{comment.position}"
            existing[key] = comment.body
        logger.debug(f"Found {len(existing)} existing comments: {existing}")
        return existing

    def calculate_line_positions(self, patch: str) -> Dict[int, int]:
        """
        Map new file line numbers to diff positions, counting all diff lines for position,
        but only tracking lines that are visible in the new version (context or additions).
        """
        positions = {}
        lines = patch.split('\n')
        position = 0
        current_line = 0

        logger.debug(f"Processing patch:\n{patch}")

        for line in lines:
            position += 1

            # Hunk header, extract the new file starting line
            if line.startswith('@@'):
                match = re.search(r'@@ -\d+(?:,\d+)? \+(\d+)', line)
                if match:
                    current_line = int(match.group(1)) - 1  # Adjust before increment
                continue

            if line.startswith('+') and not line.startswith('+++'):
                current_line += 1
                positions[current_line] = position
            elif line.startswith('-') and not line.startswith('---'):
                continue  # Old line, not present in new file
            else:
                # Context line
                current_line += 1
                positions[current_line] = position

        logger.debug(f"Line-to-position map: {json.dumps(positions, indent=2)}")
        return positions


    def find_closest_line(self, target_line: int, positions: Dict[int, int], max_distance: int = 3) -> Optional[int]:
        """
        Find the closest matching line number in the diff position map.
        Only returns a match if it's within `max_distance`.
        """
        if target_line in positions:
            return target_line

        if not positions:
            logger.debug("No available positions to match against.")
            return None

        closest_line = min(positions.keys(), key=lambda x: abs(x - target_line))
        if abs(closest_line - target_line) <= max_distance:
            logger.debug(f"Mapped target line {target_line} -> closest line {closest_line} within distance {max_distance}")
            return closest_line

        logger.debug(f"No diff line found within {max_distance} of target line {target_line}")
        return None



    def review_code(self, code: str, file_path: str) -> List[Dict]:
        """Send code to OpenAI API for review."""
        logger.info(f"Starting code review for: {file_path}")

        prompt = f"""You are a senior Drupal developer performing a code review on a pull request.

Your task:
- Identify code issues, potential bugs, and improvements.
- Follow official Drupal coding standards: https://www.drupal.org/docs/develop/standards
- Be constructive and helpful. Focus on **critical** or **architecturally important** improvements.
- Do **not** flag minor style issues unless they impact readability or maintainability.
- Respond in clear, actionable language.

Pay special attention to:
- Proper use of Drupal APIs (e.g., Entity API, Form API, Routing, Render Arrays)
- Service usage: Use dependency injection where possible, avoid using \Drupal::service() directly unless within procedural code.
- Security best practices: Never concatenate SQL directly; use the database API or entity queries.
- YAML files: Validate config/schema format. Ensure permissions and routing definitions are properly declared.
- Twig templates: Sanitize output using `|escape`, use `t()` for strings where necessary.
- Naming conventions: Ensure classes, functions, services, and hooks are named consistently with Drupal standards.
- Avoid hardcoded strings or IDs. Use constants or configuration.
- Do not repeat logic that already exists in Drupal core/contrib.
- Ensure PHPDoc and inline comments are useful and up to date.

Review this code and respond with ONLY a JSON array of found issues. For each issue include:
- line number
- explanation of the issue
- concrete code suggestion for improvement

Format EXACTLY like this JSON array, with no other text:

[
    {{
        "line": 1,
        "comment": "Description of the issue and why it should be improved",
        "suggestion": "The exact code that should replace this line"
    }}
]

If no issues are found, respond with an empty array: []

The code to review is from {file_path}:

```
{code}
```"""

        try:
            logger.debug("Sending request to OpenAI API")
            response = self.openai_client.chat.completions.create(
                model="gpt-4",
                messages=[
                    {"role": "system", "content": "You are a senior software engineer performing a code review. ..."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.2,
                max_tokens=3000,
            )
            response_text = response.choices[0].message.content

            logger.debug(f"OpenAI API raw response: {response_text}")

            try:
                review_comments = parse_ai_response(response_text)
                if not isinstance(review_comments, list):
                    logger.error("OpenAI's response is not a JSON array")
                    return []

                logger.info(f"Successfully parsed {len(review_comments)} review comments")
                return review_comments

            except json.JSONDecodeError as e:
                logger.error(f"Failed to parse OpenAI's response as JSON: {e}")
                return []

        except Exception as e:
            logger.error(f"Error during code review: {e}")
            return []

    def run_review(self):
        """Main method to run the PR review process."""
        try:
            changed_files = self.pull_request.get_files()
            draft_review_comments = []
            general_comments = []

            # Get existing comments to avoid duplicates
            existing_comments = self.get_existing_comments()

            skipped_files = []
            reviewed_files = []

            for file in changed_files:
                if file.status == "removed":
                    logger.info(f"Skipping removed file: {file.filename}")
                    continue

                # Check if file should be reviewed based on filters
                if not self.file_filter.should_review_file(file.filename):
                    logger.info(f"Skipping {file.filename} based on filter configuration")
                    skipped_files.append(file.filename)
                    continue

                reviewed_files.append(file.filename)
                logger.info(f"Reviewing: {file.filename}")

                try:
                    content = self.repo.get_contents(file.filename, ref=self.pull_request.head.sha).decoded_content.decode('utf-8')
                except Exception as e:
                    logger.error(f"Error getting file content: {e}")
                    continue

                if not file.patch:
                    logger.warning(f"No patch found for {file.filename}, skipping inline comments")
                    continue

                # Calculate line positions once per file
                line_positions = self.calculate_line_positions(file.patch)
                logger.debug(f"Line positions map for {file.filename}: {line_positions}")

                # Get review comments from OpenAI or other LLM
                file_comments = self.review_code(content, file.filename)

                for comment in file_comments:
                    line_num = comment['line']
                    position = line_positions.get(self.find_closest_line(line_num, line_positions))

                    if position is not None and isinstance(position, int):
                        comment_key = f"{file.filename}:{position}"

                        if comment_key in existing_comments:
                            logger.debug(f"Duplicate comment skipped at {comment_key}")
                            continue

                        comment_body = f"{comment['comment']}\n\n```suggestion\n{comment.get('suggestion', '')}\n```"

                        draft_review_comments.append({
                            'path': file.filename,
                            'position': position,
                            'body': comment_body
                        })
                        logger.debug(f"Queued inline comment at position {position} for {file.filename}")
                    else:
                        logger.warning(f"Invalid or unmappable line {line_num} in {file.filename}, adding as general comment")
                        comment_body = (
                            f"**In file `{file.filename}`, line {line_num}:**\n\n"
                            f"{comment['comment']}\n\n"
                            f"```suggestion\n{comment.get('suggestion', '')}\n```"
                        )
                        general_comments.append(comment_body)



            if draft_review_comments or general_comments or skipped_files:
                logger.info(f"Creating review with {len(draft_review_comments)} inline and {len(general_comments)} general comments")

                review_body = "🤖 Code Review Summary:\n\n"

                if reviewed_files:
                    review_body += f"Reviewed {len(reviewed_files)} file(s):\n"
                    for filename in reviewed_files:
                        review_body += f"- {filename}\n"

                if skipped_files:
                    review_body += f"\nSkipped {len(skipped_files)} file(s) based on filter configuration:\n"
                    for filename in skipped_files:
                        review_body += f"- {filename}\n"

                if draft_review_comments:
                    review_body += f"\nFound {len(draft_review_comments)} suggestion(s) for improvement."
                else:
                    review_body += "\n✨ Great job! The code looks clean and well-written."

                if general_comments:
                    review_body += "\n\n### Additional Comments:\n\n" + "\n\n".join(general_comments)

                commit = self.repo.get_commit(self.pull_request.head.sha)
                logger.debug(f"Inline comment payload: {json.dumps(draft_review_comments, indent=2)}")
                self.pull_request.create_review(
                    commit=commit,
                    comments=draft_review_comments,
                    body=review_body,
                    event="COMMENT"
                )
                logger.info("Review created successfully")
                logger.debug(f"Inline comment payload: {json.dumps(draft_review_comments, indent=2)}")
            else:
                logger.info("No review comments generated")

        except Exception as e:
            logger.error(f"Error in run_review: {e}", exc_info=True)
            raise


def main():
    try:
        logger.info("Starting PR review")
        reviewer = PRReviewer()
        reviewer.run_review()
        logger.info("PR review completed successfully")
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)

if __name__ == "__main__":
    main()
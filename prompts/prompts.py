from pathlib import Path

def get_prompt(prompt_name: str) -> str:
    prompts_dir = Path(__file__).parent
    with open(prompts_dir / f"{prompt_name}.md", "r") as f:
        return f.read()


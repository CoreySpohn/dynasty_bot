"""Claude AI Client for League Rumors.

Same interface as GeminiClient, so it can be picked at random alongside it
for stylistic variety in AI-powered rumor rewriting.
"""

import logging
import os
from typing import Optional

import anthropic

logger = logging.getLogger("dynasty_bot.ai")

# Configure Claude
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")


class ClaudeClient:
    """Client for Claude text generation."""

    def __init__(self, model_name: str = "claude-haiku-4-5"):
        """Initialize the Claude client.

        Args:
            model_name: Claude model to use.
        """
        self.model_name = model_name

        if ANTHROPIC_API_KEY:
            self.client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
        else:
            self.client = None
            logger.warning("ANTHROPIC_API_KEY not set, Claude features disabled")

    async def generate(self, prompt: str, max_tokens: int = 400) -> Optional[str]:
        """Run an arbitrary prompt through this backend.

        Prompt *content* belongs with the feature that needs it rather than
        being duplicated as yet another bespoke method across all three
        clients. Clients handle transport; cogs handle wording.

        Returns:
            Generated text, or None if this backend failed, so callers can
            fail over via LeagueRumors._ai_call.
        """
        if not self.client:
            logger.error("Claude client not initialized")
            return None

        try:
            response = await self.client.messages.create(
                model=self.model_name,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
            text = next((b.text for b in response.content if b.type == "text"), "")
            return text.strip() or None
        except Exception as e:
            logger.error(f"Claude API error: {e}")
            return None

    async def rewrite_as_reporter(
        self,
        rumor: str,
        reporter_name: str,
        reporter_style: str,
        team_names: Optional[list[str]] = None,
        member_context: Optional[str] = None,
        is_nfl_news: bool = False,
    ) -> Optional[str]:
        """Rewrite a user-submitted rumor in a reporter's style.

        Args:
            rumor: The original rumor text from the user.
            reporter_name: Name of the reporter persona.
            reporter_style: Style instructions for the persona.
            team_names: Optional list of team names in the league.
            member_context: Optional formatted string with member info.
            is_nfl_news: If True, this is NFL news, not fantasy league specific.

        Returns:
            The rewritten rumor in the reporter's style, or None if this
            backend could not produce one. Callers are expected to try
            another backend rather than posting a placeholder.
        """
        if not self.client:
            logger.error("Claude client not initialized")
            return None

        if is_nfl_news:
            prompt = f"""You are {reporter_name} reporting on NFL news.

STAY IN CHARACTER! This is crucial - your personality and speaking style must be UNMISTAKABLE.
{reporter_style}

Rewrite this NFL news/rumor in YOUR unique voice. Make it sound authentically like something
{reporter_name} would actually say. Use your catchphrases, mannerisms, and personality quirks.
Keep it brief (2-4 sentences). Be entertaining and dramatic.

Do not mention any fantasy football league, owners, or fantasy teams. This is pure NFL news.
Do not add any meta-commentary - just write the report.

ORIGINAL NEWS/RUMOR:
{rumor}

Write the report AS {reporter_name} (stay in character!):"""
        else:
            context = ""
            if member_context:
                context += f"\n\nLEAGUE MEMBERS:\n{member_context}"

            prompt = f"""You are {reporter_name} reporting on a fantasy football league.

STAY IN CHARACTER! This is crucial - your personality and speaking style must be UNMISTAKABLE.
{reporter_style}

Rewrite this league rumor in YOUR unique voice. Make it sound authentically like something
{reporter_name} would actually say. Use your catchphrases, mannerisms, and personality quirks.
Keep it brief (2-4 sentences). Be entertaining and dramatic.

Use people's REAL FIRST NAMES ONLY (Corey, Fuzzy, Rob Jr., James, David, Grant, Aaron, Aneesh, Rob Sr., Brendan, Noah, Kalani).
Do not mention fantasy team names - first names alone are enough.
Do not add any meta-commentary - just write the report.{context}

ORIGINAL RUMOR/INFO:
{rumor}

Write the report AS {reporter_name} (stay in character!):"""

        try:
            response = await self.client.messages.create(
                model=self.model_name,
                max_tokens=400,
                messages=[{"role": "user", "content": prompt}],
            )
            text = next((b.text for b in response.content if b.type == "text"), "")
            return text.strip() or None
        except Exception as e:
            logger.error(f"Claude API error: {e}")
            return None

    async def generate_random_rumor(
        self,
        topic: str,
        team_names: list[str],
        reporter_name: str,
        reporter_style: str,
        member_context: Optional[str] = None,
    ) -> Optional[str]:
        """Generate a completely made-up rumor for entertainment.

        Args:
            topic: General topic for the rumor.
            team_names: List of team names to potentially include.
            reporter_name: Name of the reporter persona.
            reporter_style: Style instructions for the persona.
            member_context: Optional formatted string with member info.

        Returns:
            A fabricated but entertaining rumor.
        """
        if not self.client:
            logger.error("Claude client not initialized")
            return None

        member_info = ""
        if member_context:
            member_info = f"\n\nLEAGUE MEMBERS:\n{member_context}"

        prompt = f"""You are a fantasy football league reporter creating entertaining fake rumors.
Generate a fun, dramatic, but believable-ish rumor about the topic below.

CRITICAL: Always use people's REAL FIRST NAMES ONLY (Corey, Fuzzy, Rob Jr., James, David, Grant, Aaron, Aneesh, Rob Sr., Brendan, Noah, Kalani). Do not mention fantasy team names.

Keep it brief (2-4 sentences). Make it entertaining and spicy!
This is all for fun - make it obviously tongue-in-cheek while staying in character.
Do not add any meta-commentary - just write the report.

REPORTER PERSONA:
{reporter_style}

TOPIC TO RUMOR ABOUT:
{topic}
{member_info}

Write the rumor as {reporter_name}:"""

        try:
            response = await self.client.messages.create(
                model=self.model_name,
                max_tokens=400,
                messages=[{"role": "user", "content": prompt}],
            )
            text = next((b.text for b in response.content if b.type == "text"), "")
            return text.strip() or None
        except Exception as e:
            logger.error(f"Claude API error: {e}")
            return None

    async def parse_custom_reporter(
        self, custom_prompt: str
    ) -> Optional[tuple[str, str, str]]:
        """Parse a custom reporter prompt to extract name, emoji, and style.

        Args:
            custom_prompt: User's description of the custom reporter personality.

        Returns:
            Tuple of (name, emoji, style_instructions), or None if this
            backend could not parse it, so the caller can try another.
        """
        if not self.client:
            return None

        prompt = f"""Analyze this custom reporter personality description and extract:
1. A short reporter NAME (2-3 words max, like "Drunk Pirate" or "Conspiracy Theorist")
2. A single relevant EMOJI that fits the personality
3. Detailed STYLE instructions for how this reporter should write

User's description: "{custom_prompt}"

Respond in EXACTLY this format (3 lines only):
NAME: [extracted name]
EMOJI: [single emoji]
STYLE: [detailed instructions for how this reporter talks, their catchphrases, mannerisms, etc.]"""

        try:
            response = await self.client.messages.create(
                model=self.model_name,
                max_tokens=300,
                messages=[{"role": "user", "content": prompt}],
            )
            text = next((b.text for b in response.content if b.type == "text"), "").strip()
            lines = text.split("\n")

            name = "Custom Reporter"
            emoji = "🎭"
            style = custom_prompt

            for line in lines:
                if line.startswith("NAME:"):
                    name = line.replace("NAME:", "").strip()
                elif line.startswith("EMOJI:"):
                    emoji = line.replace("EMOJI:", "").strip()
                elif line.startswith("STYLE:"):
                    style = line.replace("STYLE:", "").strip()

            return (name, emoji, style)

        except Exception as e:
            logger.error(f"Claude API error parsing custom reporter: {e}")
            return None

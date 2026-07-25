"""Gemini AI Client for League Rumors.

Handles AI-powered rewriting of user-submitted rumors into
reporter-styled posts with configurable personalities.
"""

import logging
import os
from typing import Optional

from google import genai
from google.genai import types

logger = logging.getLogger("dynasty_bot.ai")

# Configure Gemini
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")


class GeminiClient:
    """Client for Gemini AI text generation."""
    
    def __init__(self, model_name: str = "gemini-2.5-flash"):
        """Initialize the Gemini client.
        
        Args:
            model_name: Gemini model to use (flash is fast and cheap).
        """
        self.model_name = model_name
        
        if GEMINI_API_KEY:
            self.client = genai.Client(api_key=GEMINI_API_KEY)
        else:
            self.client = None
            logger.warning("GEMINI_API_KEY not set, AI features disabled")
    
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
            logger.error("Gemini client not initialized")
            return None
        
        # Build different prompts for NFL vs League rumors
        if is_nfl_news:
            # NFL news - no fantasy league context
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
            # Fantasy league rumor with context
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
            response = await self.client.aio.models.generate_content(
                model=self.model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    max_output_tokens=1000,
                    temperature=1.0,
                    # Limit thinking to prevent it from eating all output tokens
                    thinking_config=types.ThinkingConfig(thinking_budget=1024),
                ),
            )
            
            return (response.text or "").strip() or None

        except Exception as e:
            logger.error(f"Gemini API error: {e}")
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
            logger.error("Gemini client not initialized")
            return None
        
        # Build member info
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
            response = await self.client.aio.models.generate_content(
                model=self.model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    max_output_tokens=1000,
                    temperature=1.0,
                    thinking_config=types.ThinkingConfig(thinking_budget=1024),
                ),
            )
            
            return (response.text or "").strip() or None

        except Exception as e:
            logger.error(f"Gemini API error: {e}")
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
            response = await self.client.aio.models.generate_content(
                model=self.model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    max_output_tokens=300,
                    temperature=0.7,
                ),
            )
            
            text = response.text.strip()
            lines = text.split("\n")
            
            # Parse the response
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
            logger.error(f"Gemini API error parsing custom reporter: {e}")
            return None


# Singleton instance - will be created when actually used
def get_gemini_client() -> Optional[GeminiClient]:
    """Get Gemini client instance."""
    if GEMINI_API_KEY:
        return GeminiClient()
    return None

"""
Transcript Cleaner Module using OpenAI API
Cleans podcast/interview transcripts by removing filler words and improving readability
"""

import os
from typing import List, Dict, Optional
from openai import OpenAI


class TranscriptCleaner:
    """Clean transcripts using OpenAI GPT models"""

    # Pricing per 1M tokens (in USD)
    PRICING = {
        "gpt-4o-mini": {"input": 0.15, "output": 0.60},
        "gpt-4o": {"input": 2.50, "output": 10.00},
        "gpt-4-turbo": {"input": 10.00, "output": 30.00},
    }

    def __init__(self, api_key: Optional[str] = None, model: str = "gpt-4o-mini"):
        """
        Initialize the transcript cleaner

        Args:
            api_key: OpenAI API key (defaults to OPENAI_API_KEY env var)
            model: Model to use (default: gpt-4o-mini for cost efficiency)
        """
        self.api_key = api_key or os.environ.get('OPENAI_API_KEY')
        if not self.api_key:
            raise ValueError("OpenAI API key not provided. Set OPENAI_API_KEY environment variable or pass api_key parameter.")

        self.client = OpenAI(api_key=self.api_key)
        self.model = model
        self.total_input_tokens = 0
        self.total_output_tokens = 0

    def _create_system_prompt(self) -> str:
        """Create the system prompt for deep cleaning"""
        return """You are an expert transcript editor. Your task is to clean up podcast and interview transcripts to make them readable and professional while preserving the original meaning.

DEEP CLEANING INSTRUCTIONS:
1. Remove ALL filler words: um, uh, like, you know, sort of, kind of, I mean, actually, basically, literally, etc.
2. Fix ALL grammar errors while keeping the speaker's natural voice
3. Remove false starts and repetitions
4. Reorganize sentences for better flow and clarity
5. Combine short, choppy sentences into well-structured paragraphs
6. Ensure proper punctuation and capitalization
7. REMOVE ALL dialogue markers like >>, >>>, <<, <<< or similar symbols at the start of lines
8. Remove verbal tics and habits
9. Fix run-on sentences
10. Ensure smooth transitions between thoughts

CRITICAL RULES:
- Do NOT add information that wasn't in the original
- Do NOT change the speaker's core message or opinions
- Do NOT remove important content, even if it's awkwardly phrased
- Keep technical terms and names unchanged
- ALWAYS REMOVE >>, >>>, <<, <<< markers - these are NOT speaker labels
- Preserve the overall structure and flow of conversation

Output ONLY the cleaned transcript text, no explanations or commentary."""

    def clean_transcript_segment(self, transcript_text: str) -> str:
        """
        Clean a segment of transcript text

        Args:
            transcript_text: Raw transcript text to clean

        Returns:
            Cleaned transcript text
        """
        # Create the user prompt with the transcript segment
        user_prompt = f"""Clean the following transcript segment according to the deep cleaning instructions:

TRANSCRIPT TO CLEAN:
{transcript_text}

Return ONLY the cleaned transcript text."""

        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": self._create_system_prompt()},
                    {"role": "user", "content": user_prompt}
                ],
                temperature=0.3,  # Low temperature for consistent cleaning
                max_tokens=4000,
            )

            cleaned_text = response.choices[0].message.content.strip()

            # Track token usage
            self.total_input_tokens += response.usage.prompt_tokens
            self.total_output_tokens += response.usage.completion_tokens

            return cleaned_text

        except Exception as e:
            print(f"Error cleaning transcript: {e}")
            # Return original text if cleaning fails
            return transcript_text

    def clean_transcript_entries(self, entries: List[Dict], batch_size: int = 30, progress_callback=None) -> List[Dict]:
        """
        Clean a list of transcript entries by batching them for the LLM

        Args:
            entries: List of transcript entries with 'text' and 'start' keys
            batch_size: Number of entries to process in each batch (default: 30)
            progress_callback: Optional callback function for progress updates

        Returns:
            List of cleaned transcript entries
        """
        if not entries:
            return entries

        # Convert entries to text format
        original_text = "\n".join([entry.text for entry in entries])

        # If transcript is short enough, process all at once
        if len(original_text) < 3000:
            if progress_callback:
                progress_callback(0.5, "Cleaning transcript with AI...")
            cleaned_text = self.clean_transcript_segment(original_text)
            return self._reconstruct_entries(entries, cleaned_text)

        # For longer transcripts, process in batches
        cleaned_entries = []
        total_batches = (len(entries) + batch_size - 1) // batch_size

        for i in range(0, len(entries), batch_size):
            batch = entries[i:i + batch_size]
            batch_text = "\n".join([entry.text for entry in batch])

            batch_num = i // batch_size + 1
            progress = batch_num / total_batches
            message = f"Cleaning batch {batch_num}/{total_batches} with AI..."

            if progress_callback:
                progress_callback(progress, message)
            else:
                print(message)

            cleaned_text = self.clean_transcript_segment(batch_text)
            cleaned_batch = self._reconstruct_entries(batch, cleaned_text)
            cleaned_entries.extend(cleaned_batch)

        return cleaned_entries

    def _reconstruct_entries(self, original_entries: List[Dict], cleaned_text: str) -> List[Dict]:
        """
        Reconstruct transcript entries from cleaned text

        Since the LLM outputs cleaned text without timestamps, we need to
        intelligently map the cleaned text back to the original entries.

        For now, we'll use a simple approach: split by sentences and distribute evenly.
        This is a limitation that could be improved with more sophisticated parsing.
        """
        # Simple approach: split cleaned text into chunks
        # and map them to original timestamps
        import re

        # Split into sentences (rough approximation)
        sentences = re.split(r'(?<=[.!?])\s+', cleaned_text)

        # Calculate how to map sentences to original entries
        if len(sentences) <= len(original_entries):
            # Fewer or equal sentences - map 1:1 or group
            cleaned_entries = []
            sentence_idx = 0
            for orig_entry in original_entries:
                if sentence_idx < len(sentences):
                    cleaned_entries.append(
                        type('Entry', (), {'text': sentences[sentence_idx], 'start': orig_entry.start})()
                    )
                    sentence_idx += 1
                else:
                    # No more sentences, skip
                    break
            return cleaned_entries
        else:
            # More sentences than entries - distribute evenly
            cleaned_entries = []
            sentences_per_entry = len(sentences) / len(original_entries)
            for i, orig_entry in enumerate(original_entries):
                start_idx = int(i * sentences_per_entry)
                end_idx = int((i + 1) * sentences_per_entry)
                entry_text = ' '.join(sentences[start_idx:end_idx])
                cleaned_entries.append(
                    type('Entry', (), {'text': entry_text, 'start': orig_entry.start})()
                )
            return cleaned_entries

    def clean_section_text(self, section_text: str) -> str:
        """
        Clean a complete section of transcript text

        Args:
            section_text: Full text of a transcript section

        Returns:
            Cleaned text
        """
        return self.clean_transcript_segment(section_text)

    def get_usage_stats(self) -> Dict:
        """
        Get token usage statistics and estimated cost

        Returns:
            Dictionary with input_tokens, output_tokens, total_tokens, and cost_usd
        """
        pricing = self.PRICING.get(self.model, {"input": 0.15, "output": 0.60})
        input_cost = (self.total_input_tokens / 1_000_000) * pricing["input"]
        output_cost = (self.total_output_tokens / 1_000_000) * pricing["output"]
        total_cost = input_cost + output_cost

        return {
            "input_tokens": self.total_input_tokens,
            "output_tokens": self.total_output_tokens,
            "total_tokens": self.total_input_tokens + self.total_output_tokens,
            "cost_usd": round(total_cost, 4),
            "cost_cny": round(total_cost * 7.2, 2),  # Approximate CNY conversion
        }

    def get_usage_summary(self) -> str:
        """Get a human-readable summary of token usage"""
        stats = self.get_usage_stats()
        return (
            f"📊 Token Usage:\n"
            f"  • Input: {stats['input_tokens']:,} tokens\n"
            f"  • Output: {stats['output_tokens']:,} tokens\n"
            f"  • Total: {stats['total_tokens']:,} tokens\n"
            f"💰 Cost: ${stats['cost_usd']} (≈ ¥{stats['cost_cny']} CNY)"
        )


def clean_transcript_text(text: str, api_key: Optional[str] = None, model: str = "gpt-4o-mini") -> str:
    """
    Convenience function to clean transcript text

    Args:
        text: Raw transcript text
        api_key: OpenAI API key
        model: Model to use

    Returns:
        Cleaned transcript text
    """
    cleaner = TranscriptCleaner(api_key=api_key, model=model)
    return cleaner.clean_transcript_segment(text)

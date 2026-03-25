import logging
import os
import sys
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("nohuman.config")


class Settings:
    # Supabase
    SUPABASE_URL: str = os.getenv("SUPABASE_URL", "")
    SUPABASE_ANON_KEY: str = os.getenv("SUPABASE_ANON_KEY", "")
    SUPABASE_SERVICE_ROLE_KEY: str = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")

    # Deepgram
    DEEPGRAM_API_KEY: str = os.getenv("DEEPGRAM_API_KEY", "")

    # Anthropic
    ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
    CLAUDE_MODEL: str = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-5-20250929")
    HAIKU_MODEL: str = os.getenv("HAIKU_MODEL", "claude-haiku-4-5-20251001")

    # Groq (fast-path for low-latency initial response)
    GROQ_API_KEY: str = os.getenv("GROQ_API_KEY", "")
    GROQ_MODEL: str = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")

    # Twilio
    TWILIO_ACCOUNT_SID: str = os.getenv("TWILIO_ACCOUNT_SID", "")
    TWILIO_AUTH_TOKEN: str = os.getenv("TWILIO_AUTH_TOKEN", "")
    TWILIO_PHONE_NUMBER: str = os.getenv("TWILIO_PHONE_NUMBER", "")

    # Clerk
    CLERK_SECRET_KEY: str = os.getenv("CLERK_SECRET_KEY", "")
    CLERK_PUBLISHABLE_KEY: str = os.getenv("CLERK_PUBLISHABLE_KEY", "")
    CLERK_ISSUER_URL: str = os.getenv("CLERK_ISSUER_URL", "")

    # Stripe
    STRIPE_SECRET_KEY: str = os.getenv("STRIPE_SECRET_KEY", "")
    STRIPE_WEBHOOK_SECRET: str = os.getenv("STRIPE_WEBHOOK_SECRET", "")

    # ElevenLabs (text-to-speech for HumanProx mode)
    ELEVENLABS_API_KEY: str = os.getenv("ELEVENLABS_API_KEY", "")
    ELEVENLABS_VOICE_ID: str = os.getenv("ELEVENLABS_VOICE_ID", "")

    # Server
    HOST: str = os.getenv("HOST", "0.0.0.0")
    PORT: int = int(os.getenv("PORT", "8000"))
    PUBLIC_URL: str = os.getenv("PUBLIC_URL", "http://localhost:8000")

    # Environment mode
    ENV: str = os.getenv("ENV", "development")  # "development" | "production"

    @property
    def is_production(self) -> bool:
        return self.ENV.lower() in ("production", "prod")

    # CORS — allowed origins
    ALLOWED_ORIGINS: list[str] = [
        o.strip()
        for o in os.getenv(
            "ALLOWED_ORIGINS",
            "http://localhost:3000,http://localhost:8000",
        ).split(",")
        if o.strip()
    ]

    # ── Validation ──

    # Required in all environments
    _REQUIRED_ALWAYS = [
        "SUPABASE_URL",
        "SUPABASE_SERVICE_ROLE_KEY",
        "DEEPGRAM_API_KEY",
        "ANTHROPIC_API_KEY",
    ]

    # Required only in production
    _REQUIRED_PROD = [
        "CLERK_SECRET_KEY",
        "CLERK_ISSUER_URL",
        "STRIPE_SECRET_KEY",
        "STRIPE_WEBHOOK_SECRET",
    ]

    def validate(self) -> None:
        """
        Check that required env vars are set.
        In production, additionally require auth/payment keys.
        Logs warnings for missing dev vars, exits for missing prod vars.
        """
        missing = [k for k in self._REQUIRED_ALWAYS if not getattr(self, k)]
        if missing:
            logger.error(f"Missing REQUIRED env vars: {', '.join(missing)}")
            sys.exit(1)

        if self.is_production:
            prod_missing = [k for k in self._REQUIRED_PROD if not getattr(self, k)]
            if prod_missing:
                logger.error(
                    f"Missing PRODUCTION env vars: {', '.join(prod_missing)}"
                )
                sys.exit(1)
        else:
            # Warn about vars that are optional in dev but nice to have
            dev_warn = [k for k in self._REQUIRED_PROD if not getattr(self, k)]
            if dev_warn:
                logger.warning(
                    f"Unset env vars (ok for dev): {', '.join(dev_warn)}"
                )


settings = Settings()
settings.validate()

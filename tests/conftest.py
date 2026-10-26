"""테스트 실행에 필요한 최소 환경 변수.

실제 Supabase·OpenAI 호출은 하지 않는다. Settings 객체를 가져오는 테스트가
개인 .env 파일에 의존하지 않도록 더미 값을 먼저 설정한다.
"""

import os

os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_ANON_KEY", "test-anon-key")

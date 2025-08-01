import os
import io
import re
import wave
import time
import base64
import requests
from typing import List
from google.cloud import texttospeech
from google.api_core import exceptions
from google.auth import default
import google.auth.transport.requests
from snownlp import SnowNLP

# === 配置 ===
PROJECT_ID = "my-ai-250708"
CONSENT_AUDIO_FILE = "/home/molly/podcast/consent_zh.wav"
REFERENCE_AUDIO_FILE = "/home/molly/podcast/reference_zh.wav"
TEXT_TO_READ_FILE = "text_to_read_zh.txt"
SYNTHESIS_OUTPUT_PATH = "streaming_output_zh.wav"
SYNTHESIS_PROGRESS_FILE = "synthesis_progress_zh.txt"
VOICE_CLONING_KEY_FILE = "voice_cloning_key_zh.txt"
PAUSE_TAG_PATTERN = re.compile(r'\[PAUSE:(\d+)ms\]')

# === 文本切分（含 SnowNLP + PAUSE 标签）===
def split_text_for_tts(text: str, max_length: int = 120) -> List[str]:
    """
    使用 SnowNLP 对中文文本进行智能分句，保留并处理 [PAUSE:xxxms] 标签，确保不会误合成 pause 数字。
    """
    segments = []
    tokens = re.split(r'(\[PAUSE:\d+ms\])', text)  # 保留 pause 标签

    for token in tokens:
        token = token.strip()
        if not token:
            continue
        if PAUSE_TAG_PATTERN.fullmatch(token):
            segments.append(token)
        else:
            s = SnowNLP(token)
            buffer = ''
            for sentence in s.sentences:
                if len(buffer) + len(sentence) <= max_length:
                    buffer += sentence
                else:
                    if buffer:
                        segments.append(buffer)
                    buffer = sentence
            if buffer:
                segments.append(buffer)
    return segments

# === 工具函数 ===
def generate_silence(duration_ms: int, sample_rate: int = 24000) -> bytes:
    num_samples = int(sample_rate * duration_ms / 1000)
    return b'\x00\x00' * num_samples

def get_next_available_path(base_path):
    if not os.path.exists(base_path):
        return base_path
    base, ext = os.path.splitext(base_path)
    i = 1
    while True:
        new_path = f"{base}_{i}{ext}"
        if not os.path.exists(new_path):
            return new_path
        i += 1

def load_text(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read().strip()

# === streaming 合成核心逻辑 ===
def perform_voice_cloning_streaming_with_pauses(tts_client, voice_cloning_key: str, segments: List[str], output_path: str):
    voice_clone_params = texttospeech.VoiceCloneParams(voice_cloning_key=voice_cloning_key)
    streaming_config_request = texttospeech.StreamingSynthesizeRequest(
        streaming_config=texttospeech.StreamingSynthesizeConfig(
            voice=texttospeech.VoiceSelectionParams(
                language_code="cmn-CN",
                voice_clone=voice_clone_params,
            ),
            streaming_audio_config=texttospeech.StreamingAudioConfig(
                audio_encoding=texttospeech.AudioEncoding.PCM,
                sample_rate_hertz=24000,
            ),
        )
    )

    start_index = 0
    all_audio_content = []

    if os.path.exists(SYNTHESIS_PROGRESS_FILE):
        try:
            with open(SYNTHESIS_PROGRESS_FILE, "r") as f:
                start_index = int(f.read().strip()) + 1
        except Exception:
            print("⚠️ 无法读取进度，重头开始")

    if os.path.exists(output_path) and start_index > 0:
        try:
            with wave.open(output_path, 'rb') as w:
                if w.getnchannels() == 1 and w.getsampwidth() == 2 and w.getframerate() == 24000:
                    all_audio_content.append(w.readframes(w.getnframes()))
                    print(f"已加载现有音频: {output_path}")
        except Exception as e:
            print(f"⚠️ 读取音频失败: {e}")

    for i in range(start_index, len(segments)):
        segment_text = segments[i]
        match = PAUSE_TAG_PATTERN.fullmatch(segment_text)

        if match:
            pause_duration_ms = int(match.group(1))
            print(f"[{i+1}/{len(segments)}] 插入 {pause_duration_ms}ms 静音")
            all_audio_content.append(generate_silence(pause_duration_ms))
            with open(SYNTHESIS_PROGRESS_FILE, "w") as f:
                f.write(str(i))
            continue

        print(f"[{i+1}/{len(segments)}] 合成文本: {segment_text[:40]}...")

        def request_generator(text):
            yield streaming_config_request
            yield texttospeech.StreamingSynthesizeRequest(
                input=texttospeech.StreamingSynthesisInput(text=text)
            )

        success = False
        for delay in [1, 10, 60]:
            try:
                responses = tts_client.streaming_synthesize(request_generator(segment_text))
                buffer = io.BytesIO()
                for res in responses:
                    buffer.write(res.audio_content)
                all_audio_content.append(buffer.getvalue())
                with open(SYNTHESIS_PROGRESS_FILE, "w") as f:
                    f.write(str(i))
                success = True
                break
            except exceptions.GoogleAPICallError as e:
                print(f"⚠️ Google API 错误（{e.code}），{delay}s 后重试...")
                time.sleep(delay)
            except Exception as e:
                print(f"⚠️ 未知错误: {e}，{delay}s 后重试...")
                time.sleep(delay)

        if not success:
            print(f"❌ 三次重试失败，终止处理: {segment_text[:40]}...")
            return

    print("✅ 合成完成，正在写入最终音频...")
    with wave.open(output_path, 'wb') as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(24000)
        wav_file.writeframes(b''.join(all_audio_content))

    if os.path.exists(SYNTHESIS_PROGRESS_FILE):
        os.remove(SYNTHESIS_PROGRESS_FILE)
    print(f"✅ 最终音频写入完成: {output_path}")

# === VoiceCloningKey 生成 ===
def create_instant_custom_voice_key(project_id: str, reference_audio_path: str, consent_audio_path: str) -> str:
    credentials, _ = default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    credentials.refresh(google.auth.transport.requests.Request())
    access_token = credentials.token

    url = "https://texttospeech.googleapis.com/v1beta1/voices:generateVoiceCloningKey"

    def encode_audio(path):
        with open(path, "rb") as f:
            return base64.b64encode(f.read()).decode("utf-8")

    request_body = {
        "reference_audio": {
            "audio_config": {"audio_encoding": "LINEAR16", "sample_rate_hertz": 24000},
            "content": encode_audio(reference_audio_path),
        },
        "voice_talent_consent": {
            "audio_config": {"audio_encoding": "LINEAR16", "sample_rate_hertz": 24000},
            "content": encode_audio(consent_audio_path),
        },
        "consent_script": "我是此声音的拥有者并授权谷歌使用此声音创建语音合成模型",
        "language_code": "cmn-CN",
    }

    headers = {
        "Authorization": f"Bearer {access_token}",
        "x-goog-user-project": project_id,
        "Content-Type": "application/json",
    }

    response = requests.post(url, headers=headers, json=request_body)
    response.raise_for_status()
    resp_json = response.json()
    if "voiceCloningKey" not in resp_json:
        raise RuntimeError("API响应中缺失 voiceCloningKey")
    return resp_json["voiceCloningKey"]

# === 主入口 ===
def main():
    print("🟢 初始化 TTS 客户端...")
    tts_client = texttospeech.TextToSpeechClient()

    print(f"📖 加载文本文件: {TEXT_TO_READ_FILE}")
    text = load_text(TEXT_TO_READ_FILE)
    print(f"✅ 文本长度: {len(text)} 字")

    print("📚 正在分段（含静音标签处理）...")
    segments = split_text_for_tts(text, max_length=60)
    print(f"✅ 分段完成，共 {len(segments)} 段")

    if os.path.exists(VOICE_CLONING_KEY_FILE):
        with open(VOICE_CLONING_KEY_FILE, "r") as f:
            voice_cloning_key = f.read().strip()
    else:
        print("🔐 生成 Voice Cloning Key 中...")
        voice_cloning_key = create_instant_custom_voice_key(PROJECT_ID, REFERENCE_AUDIO_FILE, CONSENT_AUDIO_FILE)
        with open(VOICE_CLONING_KEY_FILE, "w") as f:
            f.write(voice_cloning_key)

    output_path = get_next_available_path(SYNTHESIS_OUTPUT_PATH)
    print(f"🎧 输出音频路径: {output_path}")

    perform_voice_cloning_streaming_with_pauses(tts_client, voice_cloning_key, segments, output_path)

if __name__ == "__main__":
    main()


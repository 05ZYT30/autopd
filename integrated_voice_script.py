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

# === 配置区 ===
PROJECT_ID = "my-ai-250708"
CONSENT_AUDIO_FILE = "/home/molly/podcast/consent.wav"
REFERENCE_AUDIO_FILE = "/home/molly/podcast/reference.wav"
TEXT_TO_READ_FILE = "text_to_read.txt"
SYNTHESIS_OUTPUT_PATH = "streaming_output.wav"
SYNTHESIS_PROGRESS_FILE = "synthesis_progress.txt"
VOICE_CLONING_KEY_FILE = "voice_cloning_key.txt"
PAUSE_TAG_PATTERN = re.compile(r'\[PAUSE:(\d+)ms\]')

# === 分割逻辑（保留暂停标签） ===
def split_text_by_pause_then_punctuation(text: str, max_length: int = 300) -> List[str]:
    pause_placeholders = []

    def pause_replacer(match):
        pause_placeholders.append(match.group(0))
        return f"[[PAUSE_{len(pause_placeholders) - 1}]]"

    text_with_placeholders = PAUSE_TAG_PATTERN.sub(pause_replacer, text)
    punctuation_pattern = re.compile(r'(.*?[\u3002\uff01\uff1f\uff0c\uff1b\uff0e])')
    pieces = [m.group(1) for m in punctuation_pattern.finditer(text_with_placeholders)]
    if not pieces:
        pieces = [text_with_placeholders]

    segments = []
    buffer = ''
    for piece in pieces:
        if len(buffer) + len(piece) <= max_length:
            buffer += piece
        else:
            if buffer:
                segments.append(buffer)
            buffer = piece
    if buffer:
        segments.append(buffer)

    final_segments = []
    for seg in segments:
        parts = re.split(r'(\[\[PAUSE_\d+\]\])', seg)
        for part in parts:
            if part.startswith('[[PAUSE_'):
                index = int(re.findall(r'\d+', part)[0])
                final_segments.append(pause_placeholders[index])
            elif part.strip():
                if len(part) > max_length:
                    for i in range(0, len(part), max_length):
                        final_segments.append(part[i:i + max_length])
                else:
                    final_segments.append(part)
    return final_segments

# === 工具函数 ===
def load_text(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read().strip()

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

# === 合成主逻辑（重试 + 断点续传 + 合并已有音频） ===
def perform_voice_cloning_streaming_with_pauses(tts_client, voice_cloning_key: str, segments: List[str], output_path: str):
    voice_clone_params = texttospeech.VoiceCloneParams(voice_cloning_key=voice_cloning_key)
    streaming_config_request = texttospeech.StreamingSynthesizeRequest(
        streaming_config=texttospeech.StreamingSynthesizeConfig(
            voice=texttospeech.VoiceSelectionParams(
                language_code="en-US",
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
            print("进度文件读取失败，重头开始合成。")

    if os.path.exists(output_path) and start_index > 0:
        try:
            with wave.open(output_path, 'rb') as w:
                if w.getnchannels() == 1 and w.getsampwidth() == 2 and w.getframerate() == 24000:
                    all_audio_content.append(w.readframes(w.getnframes()))
                    print(f"已加载现有音频内容用于合并: {output_path}")
                else:
                    print("现有音频格式不兼容，将重新写入新文件。")
        except Exception as e:
            print(f"读取音频文件失败: {e}")

    for i in range(start_index, len(segments)):
        segment_text = segments[i]
        match = PAUSE_TAG_PATTERN.match(segment_text)

        if match:
            pause_duration_ms = int(match.group(1))
            print(f"[{i+1}/{len(segments)}] 插入 {pause_duration_ms}ms 静音")
            all_audio_content.append(generate_silence(pause_duration_ms))
            with open(SYNTHESIS_PROGRESS_FILE, "w") as f:
                f.write(str(i))
        else:
            print(f"[{i+1}/{len(segments)}] 合成文本: {segment_text[:50]}...")

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
                    print(f"Google API 错误（{e.code}），{delay}s 后重试...")
                    time.sleep(delay)
                except Exception as e:
                    print(f"未知错误: {e}，{delay}s 后重试...")
                    time.sleep(delay)

            if not success:
                print(f"三次重试失败，停止处理片段: '{segment_text[:50]}...'")
                return

    print("所有片段处理完毕，合并音频...")
    with wave.open(output_path, 'wb') as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(24000)
        wav_file.writeframes(b''.join(all_audio_content))

    if os.path.exists(SYNTHESIS_PROGRESS_FILE):
        os.remove(SYNTHESIS_PROGRESS_FILE)

    print(f"✅ 最终音频成功写入: {output_path}")

# === Voice Cloning Key 生成 ===
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
        "consent_script": "I am the owner of this voice and I consent to Google using this voice to create a synthetic voice model.",
        "language_code": "en-US",
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
    print("初始化 TTS 客户端...")
    tts_client = texttospeech.TextToSpeechClient()

    print("读取文本并分段...")
    text = load_text(TEXT_TO_READ_FILE)
    segments = split_text_by_pause_then_punctuation(text)
    print(f"总共分段 {len(segments)} 个。")

    voice_cloning_key = None
    if os.path.exists(VOICE_CLONING_KEY_FILE):
        with open(VOICE_CLONING_KEY_FILE, "r") as f:
            voice_cloning_key = f.read().strip()
    else:
        print("生成新的 Voice Cloning Key...")
        voice_cloning_key = create_instant_custom_voice_key(PROJECT_ID, REFERENCE_AUDIO_FILE, CONSENT_AUDIO_FILE)
        with open(VOICE_CLONING_KEY_FILE, "w") as f:
            f.write(voice_cloning_key)

    output_path = get_next_available_path(SYNTHESIS_OUTPUT_PATH)
    print(f"输出路径: {output_path}")

    perform_voice_cloning_streaming_with_pauses(tts_client, voice_cloning_key, segments, output_path)

if __name__ == "__main__":
    main()


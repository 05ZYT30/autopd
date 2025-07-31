import os
import io
import wave
import re
import base64
import requests
from typing import List
from google.cloud import texttospeech
from google.api_core import exceptions
from google.auth import default
import google.auth.transport.requests

PROJECT_ID = "my-ai-250708"
CONSENT_AUDIO_FILE = "/home/molly/podcast/consent.wav"
REFERENCE_AUDIO_FILE = "/home/molly/podcast/reference.wav"
TEXT_TO_READ_FILE = "text_to_read.txt"
SYNTHESIS_OUTPUT_PATH = "streaming_output.wav"
SYNTHESIS_PROGRESS_FILE = "synthesis_progress.txt"
VOICE_CLONING_KEY_FILE = "voice_cloning_key.txt"

def split_ssml_by_breaks_with_pauses(ssml_text: str, threshold_ms: int = 5000):
    """
    返回 [(纯文本, pause_ms), ...]，pause_ms是当前段后面的停顿时长（毫秒），
    如果最后一段没有停顿，pause_ms为0。
    """
    body = re.sub(r"</?speak>", "", ssml_text.strip(), flags=re.IGNORECASE)
    break_pattern = re.compile(r'<break\s+time="(\d+)ms"\s*/>', re.IGNORECASE)

    segments = []
    last_index = 0
    last_pause = 0

    for match in break_pattern.finditer(body):
        time_ms = int(match.group(1))
        start, end = match.span()

        if time_ms >= threshold_ms:
            chunk = body[last_index:start].strip()
            if chunk:
                chunk_text = strip_ssml_tags(chunk)
                if chunk_text:
                    segments.append((chunk_text, time_ms))
            last_index = end
            last_pause = 0
        else:
            # 低于阈值的break不拆分，直接包含到文本中
            pass

    # 最后一段没有后续停顿，设为0
    remaining = body[last_index:].strip()
    if remaining:
        chunk_text = strip_ssml_tags(remaining)
        if chunk_text:
            segments.append((chunk_text, 0))
    return segments


def generate_silence(duration_ms: int, sample_rate: int = 24000) -> bytes:
    """
    生成指定毫秒数的单声道16位PCM静音数据。
    """
    num_samples = int(sample_rate * duration_ms / 1000)
    silence = b'\x00\x00' * num_samples  # 16位即2字节，每个采样2字节
    return silence

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

def perform_voice_cloning_streaming_with_pauses(tts_client, voice_cloning_key: str, segments_with_pauses, output_path: str):
    voice_clone_params = texttospeech.VoiceCloneParams(voice_cloning_key=voice_cloning_key)
    streaming_config = texttospeech.StreamingSynthesizeConfig(
        voice=texttospeech.VoiceSelectionParams(
            language_code="en-US",
            voice_clone=voice_clone_params,
        ),
        streaming_audio_config=texttospeech.StreamingAudioConfig(
            audio_encoding=texttospeech.AudioEncoding.PCM,
            sample_rate_hertz=24000,
        ),
    )
    config_request = texttospeech.StreamingSynthesizeRequest(streaming_config=streaming_config)

    start_index = 0
    if os.path.exists(SYNTHESIS_PROGRESS_FILE):
        try:
            with open(SYNTHESIS_PROGRESS_FILE, "r") as f:
                start_index = int(f.read().strip()) + 1
        except Exception:
            print("进度文件读取失败，重头开始合成")

    segments_to_process = segments_with_pauses[start_index:]

    existing_audio = io.BytesIO()
    if os.path.exists(output_path):
        try:
            with wave.open(output_path, 'rb') as w:
                if w.getnchannels() == 1 and w.getsampwidth() == 2 and w.getframerate() == 24000:
                    existing_audio.write(w.readframes(w.getnframes()))
                else:
                    print("现有音频文件格式不兼容，将重写文件。")
                    existing_audio = io.BytesIO()
        except Exception as e:
            print(f"读取现有音频文件异常，重写文件: {e}")

    segment_audio = [existing_audio.getvalue()]

    def request_generator():
        yield config_request
        for i, (segment_text, _) in enumerate(segments_to_process):
            print(f"合成段 {start_index + i}: {segment_text[:40]}...")
            yield texttospeech.StreamingSynthesizeRequest(
                input=texttospeech.StreamingSynthesisInput(text=segment_text)
            )

    try:
        responses = tts_client.streaming_synthesize(request_generator())
        for i, res in enumerate(responses):
            segment_audio.append(res.audio_content)
            with open(SYNTHESIS_PROGRESS_FILE, "w") as f:
                f.write(str(start_index + i))
    except exceptions.GoogleAPICallError as e:
        print(f"Google API 调用错误: {e}")
        return
    except Exception as e:
        import traceback
        print(f"未知错误发生: {e}")
        traceback.print_exc()
        return

    # 插入静音片段
    final_audio = io.BytesIO()
    for i, audio_bytes in enumerate(segment_audio[1:]):  # 跳过第一段是已有音频
        final_audio.write(audio_bytes)
        # 当前段结束后插入停顿静音（除了最后一段）
        if i < len(segments_to_process):
            pause_ms = segments_to_process[i][1]
            if pause_ms > 0:
                silence_bytes = generate_silence(pause_ms)
                final_audio.write(silence_bytes)

    # 加上已有音频内容
    final_audio_data = segment_audio[0] + final_audio.getvalue()

    # 写入wav文件
    try:
        with wave.open(output_path, 'wb') as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(24000)
            wav_file.writeframes(final_audio_data)
    except Exception as e:
        print(f"写入最终音频文件失败: {e}")
        return

    if os.path.exists(SYNTHESIS_PROGRESS_FILE):
        os.remove(SYNTHESIS_PROGRESS_FILE)
    print(f"最终音频写入: {output_path}")

def load_ssml_text(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read().strip()

def strip_ssml_tags(ssml_text: str) -> str:
    # 移除<speak>和所有xml标签，保留纯文本
    text = re.sub(r"</?speak>", "", ssml_text, flags=re.IGNORECASE)
    # 移除所有其它xml标签，比如<break time="xxxms"/>
    text = re.sub(r"<[^>]+>", "", text)
    return text.strip()

def split_ssml_by_breaks(ssml_text: str, threshold_ms: int = 5000) -> List[str]:
    # 分段依据break标签，但返回的是纯文本片段
    body = re.sub(r"</?speak>", "", ssml_text.strip(), flags=re.IGNORECASE)
    break_pattern = re.compile(r'<break\s+time="(\d+)ms"\s*/>', re.IGNORECASE)
    segments = []
    last_index = 0
    for match in break_pattern.finditer(body):
        time_ms = int(match.group(1))
        start, end = match.span()
        if time_ms >= threshold_ms:
            chunk = body[last_index:start].strip()
            if chunk:
                chunk_text = strip_ssml_tags(chunk)
                if chunk_text:
                    segments.append(chunk_text)
            last_index = end
    remaining = body[last_index:].strip()
    if remaining:
        chunk_text = strip_ssml_tags(remaining)
        if chunk_text:
            segments.append(chunk_text)
    return segments

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
    json_resp = response.json()
    if "voiceCloningKey" not in json_resp:
        raise RuntimeError(f"无法获取 voiceCloningKey，API响应: {json_resp}")
    return json_resp["voiceCloningKey"]

def perform_voice_cloning_streaming(tts_client, voice_cloning_key: str, text_segments: List[str], output_path: str):
    voice_clone_params = texttospeech.VoiceCloneParams(voice_cloning_key=voice_cloning_key)
    streaming_config = texttospeech.StreamingSynthesizeConfig(
        voice=texttospeech.VoiceSelectionParams(
            language_code="en-US",
            voice_clone=voice_clone_params,
        ),
        streaming_audio_config=texttospeech.StreamingAudioConfig(
            audio_encoding=texttospeech.AudioEncoding.PCM,
            sample_rate_hertz=24000,
        ),
    )
    config_request = texttospeech.StreamingSynthesizeRequest(streaming_config=streaming_config)

    start_index = 0
    if os.path.exists(SYNTHESIS_PROGRESS_FILE):
        try:
            with open(SYNTHESIS_PROGRESS_FILE, "r") as f:
                start_index = int(f.read().strip()) + 1
        except Exception:
            print("进度文件读取失败，重头开始合成")

    segments_to_process = text_segments[start_index:]

    existing_audio = io.BytesIO()
    if os.path.exists(output_path):
        try:
            with wave.open(output_path, 'rb') as w:
                if w.getnchannels() == 1 and w.getsampwidth() == 2 and w.getframerate() == 24000:
                    existing_audio.write(w.readframes(w.getnframes()))
                else:
                    print("现有音频文件格式不兼容，将重写文件。")
                    existing_audio = io.BytesIO()
        except Exception as e:
            print(f"读取现有音频文件异常，重写文件: {e}")

    segment_audio = [existing_audio.getvalue()]

    def request_generator():
        yield config_request
        for i, segment in enumerate(segments_to_process):
            print(f"合成段 {start_index + i}: {segment[:40]}...")
            yield texttospeech.StreamingSynthesizeRequest(
                input=texttospeech.StreamingSynthesisInput(text=segment)
            )

    try:
        responses = tts_client.streaming_synthesize(request_generator())
        for i, res in enumerate(responses):
            segment_audio.append(res.audio_content)
            with open(SYNTHESIS_PROGRESS_FILE, "w") as f:
                f.write(str(start_index + i))
    except exceptions.GoogleAPICallError as e:
        print(f"Google API 调用错误: {e}")
        return
    except Exception as e:
        import traceback
        print(f"未知错误发生: {e}")
        traceback.print_exc()
        return

    combined_audio = io.BytesIO()
    for seg in segment_audio:
        combined_audio.write(seg)

    try:
        with wave.open(output_path, 'wb') as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(24000)
            wav_file.writeframes(combined_audio.getvalue())
    except Exception as e:
        print(f"写入最终音频文件失败: {e}")
        return

    if os.path.exists(SYNTHESIS_PROGRESS_FILE):
        os.remove(SYNTHESIS_PROGRESS_FILE)
    print(f"最终音频写入: {output_path}")

def main():
    print("初始化 TTS 客户端...")
    tts_client = texttospeech.TextToSpeechClient()

    print("读取 SSML 文本并分段（带停顿）...")
    ssml_text = load_ssml_text(TEXT_TO_READ_FILE)
    segments_with_pauses = split_ssml_by_breaks_with_pauses(ssml_text, threshold_ms=5000)

    if os.path.exists(VOICE_CLONING_KEY_FILE):
        with open(VOICE_CLONING_KEY_FILE, "r") as f:
            voice_cloning_key = f.read().strip()
    else:
        voice_cloning_key = create_instant_custom_voice_key(
            project_id=PROJECT_ID,
            reference_audio_path=REFERENCE_AUDIO_FILE,
            consent_audio_path=CONSENT_AUDIO_FILE,
        )
        with open(VOICE_CLONING_KEY_FILE, "w") as f:
            f.write(voice_cloning_key)

    output_path = get_next_available_path(SYNTHESIS_OUTPUT_PATH)

    perform_voice_cloning_streaming_with_pauses(
        tts_client=tts_client,
        voice_cloning_key=voice_cloning_key,
        segments_with_pauses=segments_with_pauses,
        output_path=SYNTHESIS_OUTPUT_PATH
    )


if __name__ == "__main__":
    main()


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

# 正则表达式用于匹配自定义停顿标签
PAUSE_TAG_PATTERN = re.compile(r'\[PAUSE:(\d+)ms\]')

def split_text_by_punctuation(text: str) -> List[str]:
    """
    根据英文标点符号和自定义停顿标签分割文本，保留标点和标签。
    这个版本会更直接地匹配并分离出PAUSE标签和标点结尾的句子。
    """
    # 匹配三种情况：
    # 1. 以 .!? 结尾的句子 (group 1)
    # 2. 自定义的 [PAUSE:Xms] 标签 (group 2)
    # 3. 任何非空白字符序列，直到下一个匹配点 (group 3) - 用于捕获没有标点或PAUSE的末尾片段
    # | 符号表示“或”
    # (?P<segment>...) 是命名捕获组，方便后续处理
    pattern = re.compile(r'(?P<sentence>[^.!?]+[.!?])|(?P<pause_tag>\[PAUSE:\d+ms\])|(?P<word_seq>[^\s\[.]*\S+)', re.MULTILINE)
    
    segments = []
    last_end = 0
    for match in pattern.finditer(text):
        # 捕获匹配之前的任何文本（例如，如果开头没有标点或标签）
        if match.start() > last_end:
            leading_text = text[last_end:match.start()].strip()
            if leading_text:
                segments.append(leading_text)

        # 添加匹配到的部分
        matched_content = match.group(0).strip()
        if matched_content:
            segments.append(matched_content)
        
        last_end = match.end()

    # 添加文本末尾剩余的部分（如果pattern没有覆盖）
    if last_end < len(text):
        trailing_text = text[last_end:].strip()
        if trailing_text:
            segments.append(trailing_text)
            
    # 过滤掉空字符串
    return [s for s in segments if s]


def generate_silence(duration_ms: int, sample_rate: int = 24000) -> bytes:
    """
    生成指定毫秒数的单声道16位PCM静音数据。
    """
    num_samples = int(sample_rate * duration_ms / 1000)
    # 16-bit PCM (2 bytes per sample)
    silence = b'\x00\x00' * num_samples
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

def perform_voice_cloning_streaming_with_pauses(tts_client, voice_cloning_key: str, segments: List[str], output_path: str):
    voice_clone_params = texttospeech.VoiceCloneParams(voice_cloning_key=voice_cloning_key)
    # 配置保持不变
    streaming_config_request = texttospeech.StreamingSynthesizeRequest(
        streaming_config=texttospeech.StreamingSynthesizeConfig(
            voice=texttospeech.VoiceSelectionParams(
                language_code="en-US",
                voice_clone=voice_clone_params, # voice_clone_params 应该在外部定义或传入
            ),
            streaming_audio_config=texttospeech.StreamingAudioConfig(
                audio_encoding=texttospeech.AudioEncoding.PCM,
                sample_rate_hertz=24000,
            ),
        )
    )
    


    start_index = 0
    if os.path.exists(SYNTHESIS_PROGRESS_FILE):
        try:
            with open(SYNTHESIS_PROGRESS_FILE, "r") as f:
                start_index = int(f.read().strip()) + 1
        except Exception:
            print("进度文件读取失败，重头开始合成。")

    # 尝试加载现有音频以实现断点续传
    existing_audio = io.BytesIO()
    if os.path.exists(output_path):
        try:
            with wave.open(output_path, 'rb') as w:
                # 检查格式兼容性
                if w.getnchannels() == 1 and w.getsampwidth() == 2 and w.getframerate() == 24000:
                    existing_audio.write(w.readframes(w.getnframes()))
                    print(f"已加载现有音频文件: {output_path}")
                else:
                    print("现有音频文件格式不兼容，将重写文件。")
                    existing_audio = io.BytesIO() # 重置为空
        except Exception as e:
            print(f"读取现有音频文件异常，重写文件: {e}")
            existing_audio = io.BytesIO() # 重置为空
            
    # 用于存储所有音频数据的列表
    all_audio_content = [existing_audio.getvalue()]

    current_segment_idx_for_progress = start_index # 真正用于进度跟踪的索引

    for i in range(start_index, len(segments)):
        segment_text = segments[i]
        match = PAUSE_TAG_PATTERN.match(segment_text)

        if match:
            # 这是一个自定义停顿标签
            pause_duration_ms = int(match.group(1))
            print(f"[{i+1}/{len(segments)}] 插入 {pause_duration_ms}ms 静音")
            silent_data = generate_silence(pause_duration_ms, sample_rate=24000)
            all_audio_content.append(silent_data)
        else:
            # 这是一个文本片段，需要进行 TTS 合成
            print(f"[{i+1}/{len(segments)}] 合成文本: {segment_text[:50]}...")
            
            # 为当前文本片段创建一个新的请求生成器
            def current_text_request_generator(text_to_synthesize):
                yield streaming_config_request # 每次合成新片段时发送配置
                yield texttospeech.StreamingSynthesizeRequest(
                    input=texttospeech.StreamingSynthesisInput(text=text_to_synthesize)
                )

            try:
                # 对当前文本片段执行流式合成
                responses_for_segment = tts_client.streaming_synthesize(current_text_request_generator(segment_text))
                
                segment_audio_buffer = io.BytesIO()
                for res in responses_for_segment:
                    segment_audio_buffer.write(res.audio_content)
                
                all_audio_content.append(segment_audio_buffer.getvalue())
                
                # 成功合成后更新进度文件
                with open(SYNTHESIS_PROGRESS_FILE, "w") as f:
                    f.write(str(i)) # 记录当前完成的片段索引

            except exceptions.GoogleAPICallError as e:
                print(f"Google API 调用错误，无法合成片段 '{segment_text[:50]}...': {e}")
                # 遇到错误时停止并保留进度
                return
            except Exception as e:
                import traceback
                print(f"未知错误发生，无法合成片段 '{segment_text[:50]}...': {e}")
                traceback.print_exc()
                # 遇到错误时停止并保留进度
                return

    print("所有片段处理完毕，合并音频...")
    combined_audio = io.BytesIO()
    for audio_data in all_audio_content:
        combined_audio.write(audio_data)

    try:
        with wave.open(output_path, 'wb') as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(24000)
            wav_file.writeframes(combined_audio.getvalue())
    except Exception as e:
        print(f"写入最终音频文件失败: {e}")
        return

    # 所有合成完成后，删除进度文件
    if os.path.exists(SYNTHESIS_PROGRESS_FILE):
        os.remove(SYNTHESIS_PROGRESS_FILE)
    print(f"最终音频成功写入: {output_path}")

def load_text(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read().strip()

def main():
    print("初始化 TTS 客户端...")
    tts_client = texttospeech.TextToSpeechClient()

    print("读取文本并基于标点和自定义标签分段...")
    text = load_text(TEXT_TO_READ_FILE)
    segments = split_text_by_punctuation(text)
    print(f"总共分段 {len(segments)} 个。")

    voice_cloning_key = None
    if os.path.exists(VOICE_CLONING_KEY_FILE):
        with open(VOICE_CLONING_KEY_FILE, "r") as f:
            voice_cloning_key = f.read().strip()
        print("已从文件加载 Voice Cloning Key。")
    else:
        print("未找到 Voice Cloning Key，正在生成新的...")
        try:
            voice_cloning_key = create_instant_custom_voice_key(
                project_id=PROJECT_ID,
                reference_audio_path=REFERENCE_AUDIO_FILE,
                consent_audio_path=CONSENT_AUDIO_FILE,
            )
            with open(VOICE_CLONING_KEY_FILE, "w") as f:
                f.write(voice_cloning_key)
            print("Voice Cloning Key 已生成并保存。")
        except Exception as e:
            print(f"生成 Voice Cloning Key 失败: {e}")
            return

    output_path = get_next_available_path(SYNTHESIS_OUTPUT_PATH)
    print(f"输出音频路径设置为: {output_path}")

    perform_voice_cloning_streaming_with_pauses(
        tts_client=tts_client,
        voice_cloning_key=voice_cloning_key,
        segments=segments,
        output_path=output_path
    )


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

    try:
        response = requests.post(url, headers=headers, json=request_body)
        response.raise_for_status()
    except requests.exceptions.HTTPError as http_err:
        print(f"请求失败，状态码: {response.status_code}")
        print("响应内容:")
        try:
            print(response.json())
        except Exception:
            print(response.text)
        raise http_err

    json_resp = response.json()
    if "voiceCloningKey" not in json_resp:
        raise RuntimeError(f"无法获取 voiceCloningKey，API响应: {json_resp}")
    return json_resp["voiceCloningKey"]

if __name__ == "__main__":
    main()

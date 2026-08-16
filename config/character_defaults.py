# -*- coding: utf-8 -*-
# Copyright 2025-2026 Project N.E.K.O. Team
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Default character, avatar, lighting, and localized profile configuration."""

from copy import deepcopy

from .prompts.prompts_chara import (
    get_lanlan_prompt,
    is_default_prompt as is_default_prompt,
    lanlan_prompt,
)
from .application import logger

CONFIG_FILES = [
    'characters.json',
    'core_config.json',
    'user_preferences.json',
    'voice_storage.json',
]

DEFAULT_MASTER_TEMPLATE = {
    "档案名": "哥哥",
    "性别": "男",
    "昵称": "哥哥",
}

# Public character assets are operator-installed and licensed separately.
DEFAULT_LIVE2D_MODEL_NAME = ""
DEFAULT_LIVE2D_MODEL_PATH = ""
BUILTIN_LIVE2D_MODEL_NAMES: tuple[str, ...] = ()

DEFAULT_LANLAN_TEMPLATE = {
    "Lanlan": {
        "性别": "女",
        "年龄": 15,
        "昵称": "Lanlan",
        "_reserved": {
            "voice_id": "",
            "system_prompt": lanlan_prompt,
            "avatar": {
                "model_type": "live2d",
                "asset_source": "local",
                "live2d": {
                    "model_path": "",
                },
            },
        },
    }
}

DEFAULT_CHARACTERS_CONFIG = {
    "主人": deepcopy(DEFAULT_MASTER_TEMPLATE),
    "猫娘": deepcopy(DEFAULT_LANLAN_TEMPLATE),
    "当前猫娘": next(iter(DEFAULT_LANLAN_TEMPLATE.keys()), "")
}


# 内容值翻译映射（仅翻译值，键名保持中文不变，因为系统内部依赖这些键名）
_VALUE_TRANSLATIONS = {
    'en': {
        '哥哥': 'Brother',
        '男': 'Male',
        '女': 'Female',
        'T酱, 小T': 'T-chan, Little T',
    },
    'ja': {
        '哥哥': 'お兄ちゃん',
        '男': '男性',
        '女': '女性',
        'T酱, 小T': 'Tちゃん, 小T',
    },
    'zh-TW': {
        '哥哥': '哥哥',
        '男': '男',
        '女': '女',
        'T酱, 小T': 'T醬, 小T',
    },
    'ru': {
        '哥哥': 'Братик',
        '男': 'Мужской',
        '女': 'Женский',
        'T酱, 小T': 'Тян-тян, малышка Т',
    },
    'es': {
        '哥哥': 'Hermano',
        '男': 'Masculino',
        '女': 'Femenino',
        'T酱, 小T': 'T-chan, Pequeña T',
    },
    'pt': {
        '哥哥': 'Irmão',
        '男': 'Masculino',
        '女': 'Feminino',
        'T酱, 小T': 'T-chan, Pequena T',
    },
    # zh 和 zh-CN 使用原始中文值（不需要翻译）
}


def get_localized_default_characters(language: str | None = None) -> dict:
    """
    Get the localized default character configuration.

    Translates content values based on the configured or system language.
    Note: legacy key names remain unchanged because internal code depends on them.
    Only used when characters.json is created for the first time.

    Args:
        language: Language code ('en', 'ja', 'zh', 'zh-CN', 'zh-TW').
                  If None, fetched from the runtime language or defaults to 'zh-CN'.

    Returns:
        Localized copy of DEFAULT_CHARACTERS_CONFIG
    """
    # 获取语言代码
    if language is None:
        try:
            from config._runtime import resolve_global_language, normalize_language_code
            runtime_language = resolve_global_language(default='zh-CN')
            language = normalize_language_code(runtime_language, format='full')
        except Exception as e:
            logger.warning(f"获取运行时语言失败: {e}，使用默认中文")
            language = 'zh-CN'

    # 获取翻译映射
    value_trans = _VALUE_TRANSLATIONS.get(language)

    # 尝试根据前缀匹配
    if value_trans is None:
        lang_lower = language.lower()
        if lang_lower.startswith('zh'):
            if 'tw' in lang_lower:
                value_trans = _VALUE_TRANSLATIONS.get('zh-TW')
            # 简体中文不需要翻译
        elif lang_lower.startswith('ja'):
            value_trans = _VALUE_TRANSLATIONS.get('ja')
        elif lang_lower.startswith('en'):
            value_trans = _VALUE_TRANSLATIONS.get('en')
        elif lang_lower.startswith('ru'):
            value_trans = _VALUE_TRANSLATIONS.get('ru')
        elif lang_lower.startswith('es'):
            value_trans = _VALUE_TRANSLATIONS.get('es')
        elif lang_lower.startswith('pt'):
            value_trans = _VALUE_TRANSLATIONS.get('pt')

    # 如果不需要翻译显示字段（简体中文/韩语等），仍需本地化 system_prompt
    if value_trans is None:
        result = deepcopy(DEFAULT_CHARACTERS_CONFIG)
        for char_config in result.get('猫娘', {}).values():
            reserved = char_config.get('_reserved')
            if isinstance(reserved, dict) and 'system_prompt' in reserved:
                reserved['system_prompt'] = get_lanlan_prompt(language)
        return result

    def translate_value(val):
        """Translate a value (only string types are translated)"""
        if isinstance(val, str):
            return value_trans.get(val, val)
        return val

    # 构建本地化配置（键名保持不变，只翻译值）
    result = {}

    # 本地化主人模板
    master = deepcopy(DEFAULT_MASTER_TEMPLATE)
    localized_master = {}
    for key, value in master.items():
        localized_master[key] = translate_value(value)
    result['主人'] = localized_master

    # 本地化猫娘模板
    catgirl_data = deepcopy(DEFAULT_LANLAN_TEMPLATE)
    localized_catgirl = {}
    for char_name, char_config in catgirl_data.items():
        localized_config = {}
        for key, value in char_config.items():
            localized_config[key] = translate_value(value)
        reserved = localized_config.get('_reserved')
        if isinstance(reserved, dict) and 'system_prompt' in reserved:
            reserved['system_prompt'] = get_lanlan_prompt(language)
        localized_catgirl[char_name] = localized_config
    result['猫娘'] = localized_catgirl

    result['当前猫娘'] = next(iter(catgirl_data.keys()), "")

    return result

import json
import os
import re
from typing import Dict, List, Optional, Sequence

from presidio_anonymizer.core.text_replace_builder import TextReplaceBuilder
from presidio_anonymizer.entities import PIIEntity, RecognizerResult

from ..exception import LLMGuardValidationError
from ..util import get_logger
from ..vault import Vault
from .anonymize_helpers import (
    BERT_BASE_NER_CONF,
    get_analyzer,
    get_fake_value,
    get_transformers_recognizer,
)
from .base import Scanner

LOGGER = get_logger()

sensitive_patterns_path = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..",
    "resources",
    "sensisitive_patterns.json",
)

default_entity_types = [
    "CREDIT_CARD",
    "CRYPTO",
    "EMAIL_ADDRESS",
    "IBAN_CODE",
    "IP_ADDRESS",
    "PERSON",
    "PHONE_NUMBER",
    "US_SSN",
    "US_BANK_NUMBER",
    "CREDIT_CARD_RE",
    "UUID",
    "EMAIL_ADDRESS_RE",
    "US_SSN_RE",
]

ALL_SUPPORTED_LANGUAGES = ["en", "zh"]


class Anonymize(Scanner):
    """
    Anonymize sensitive data in the text using NLP (English only) and predefined regex patterns.

    Anonymizes detected entities with placeholders like [REDACTED_PERSON_1] and stores the real values in a Vault.
    Deanonymizer can be used to replace the placeholders back to their original values.
    """

    def __init__(
        self,
        vault: Vault,
        *,
        hidden_names: Optional[Sequence[str]] = None,
        allowed_names: Optional[Sequence[str]] = None,
        entity_types: Optional[Sequence[str]] = None,
        preamble: str = "",
        regex_pattern_groups_path: str = sensitive_patterns_path,
        use_faker: bool = False,
        recognizer_conf: Optional[Dict] = BERT_BASE_NER_CONF,
        threshold: float = 0,
        use_onnx: bool = False,
        language: str = "en",
        model_kwargs: Optional[Dict] = None,
        pipeline_kwargs: Optional[Dict] = None,
    ):
        """
        Initialize an instance of Anonymize class.

        Parameters:
            vault (Vault): A vault instance to store the anonymized data.
            hidden_names (Optional[Sequence[str]]): List of names to be anonymized e.g. [REDACTED_CUSTOM_1].
            allowed_names (Optional[Sequence[str]]): List of names allowed in the text without anonymizing.
            entity_types (Optional[Sequence[str]]): List of entity types to be detected. If not provided, defaults to all.
            preamble (str): Text to prepend to sanitized prompt. If not provided, defaults to an empty string.
            regex_pattern_groups_path (str): Path to a JSON file with regex pattern groups. If not provided, defaults to sensisitive_patterns.json.
            use_faker (bool): Whether to use faker instead of placeholders in applicable cases. If not provided, defaults to False, replaces with placeholders [REDACTED_PERSON_1].
            recognizer_conf (Optional[Dict]): Configuration to recognize PII data. Default is dslim/bert-base-NER.
            threshold (float): Acceptance threshold. Default is 0.
            use_onnx (bool): Whether to use ONNX runtime for inference. Default is False.
            language (str): Language of the anonymize detect. Default is "en".
            model_kwargs (Optional[Dict]): Keyword arguments passed to the model.
            pipeline_kwargs (Optional[Dict]): Keyword arguments passed to the pipeline.
        """

        if language not in ALL_SUPPORTED_LANGUAGES:
            raise LLMGuardValidationError(
                f"Language must be in the list of allowed: {ALL_SUPPORTED_LANGUAGES}"
            )

        os.environ["TOKENIZERS_PARALLELISM"] = "false"  # Disables huggingface/tokenizers warning

        if not entity_types:
            LOGGER.debug(
                "No entity types provided, using default", default_entities=default_entity_types
            )
            entity_types = default_entity_types.copy()

        entity_types.append("CUSTOM")

        if not hidden_names:
            hidden_names = []

        self._vault = vault
        self._entity_types = entity_types
        self._allowed_names = allowed_names
        self._preamble = preamble
        self._use_faker = use_faker
        self._threshold = threshold
        self._language = language

        transformers_recognizer = get_transformers_recognizer(
            recognizer_conf=recognizer_conf,
            use_onnx=use_onnx,
            supported_language=language,
            model_kwargs=model_kwargs,
            pipeline_kwargs=pipeline_kwargs,
        )

        self._analyzer = get_analyzer(
            recognizer=transformers_recognizer,
            regex_groups=Anonymize.get_regex_patterns(regex_pattern_groups_path),
            custom_names=hidden_names,
            supported_languages=list(set(["en", language])),
        )

    @staticmethod
    def get_regex_patterns(json_path: str) -> List[dict]:
        """
        Load regex patterns from a specified JSON file.

        Parameters:
            json_path (str): Path to the JSON file containing regex patterns.

        Returns:
            List[dict]: List of regex patterns with each dictionary containing "name", "expressions", "context", and "score".
                        Returns an empty list if file not found or parsing error occurred.
        """
        regex_groups = []
        try:
            with open(json_path, "r") as myfile:
                pattern_groups_raw = json.load(myfile)
            for group in pattern_groups_raw:
                regex_groups.append(
                    {
                        "name": group["name"].upper(),
                        "expressions": group.get("expressions", []),
                        "context": group.get("context", []),
                        "score": group.get("score", 0.75),
                        "languages": group.get("languages", ["en"]),
                        "reuse": group.get("reuse", False),
                    }
                )
                LOGGER.debug("Loaded regex pattern", group_name=group["name"])
        except FileNotFoundError:
            LOGGER.warning("Could not find file", path=json_path)
        except json.decoder.JSONDecodeError as json_error:
            LOGGER.warning("Could not parse file", path=json_path, error=json_error)
        return regex_groups

    @staticmethod
    def _remove_conflicts_and_get_text_manipulation_data(
        analyzer_results: List[RecognizerResult],
    ) -> List[RecognizerResult]:
        """
        Remove conflicting and redundant detections from the analyzer results.
        """
        tmp_analyzer_results = []
        # This list contains all elements which we need to check a single result
        # against. If a result is dropped, it can also be dropped from this list
        # since it is intersecting with another result, and we selected the other one.
        other_elements = analyzer_results.copy()
        for result in analyzer_results:
            other_elements.remove(result)

            is_merge_same_entity_type = False
            for other_element in other_elements:
                if other_element.entity_type != result.entity_type:
                    continue
                if result.intersects(other_element) == 0:
                    continue

                other_element.start = min(result.start, other_element.start)
                other_element.end = max(result.end, other_element.end)
                other_element.score = max(result.score, other_element.score)
                is_merge_same_entity_type = True
                break
            if not is_merge_same_entity_type:
                other_elements.append(result)
                tmp_analyzer_results.append(result)
            else:
                LOGGER.debug("Removing element the results list due to merge", result=result)

        unique_text_metadata_elements = []
        # This list contains all elements which we need to check a single result
        # against. If a result is dropped, it can also be dropped from this list
        # since it is intersecting with another result, and we selected the other one.
        other_elements = tmp_analyzer_results.copy()
        for result in tmp_analyzer_results:
            other_elements.remove(result)
            result_conflicted = Anonymize.__is_result_conflicted_with_other_elements(
                other_elements, result
            )
            if not result_conflicted:
                other_elements.append(result)
                unique_text_metadata_elements.append(result)
            else:
                LOGGER.debug(
                    "Removing element from the results list due to conflict", result=result
                )
        return unique_text_metadata_elements

    @staticmethod
    def __is_result_conflicted_with_other_elements(other_elements, result):
        """
        Check if the given result conflicts with any other elements.
        """
        return any([result.has_conflict(other_element) for other_element in other_elements])

    @staticmethod
    def _merge_entities_with_whitespace_between(
        text: str, analyzer_results: List[RecognizerResult]
    ) -> List[RecognizerResult]:
        """
        Merge adjacent entities of the same type separated by whitespace.
        """
        merged_results = []
        prev_result = None
        for result in analyzer_results:
            if prev_result is not None:
                if prev_result.entity_type == result.entity_type:
                    if re.search(r"^( )+$", text[prev_result.end : result.start]):
                        merged_results.remove(prev_result)
                        result.start = prev_result.start
            merged_results.append(result)
            prev_result = result
        return merged_results

    @staticmethod
    def _get_entity_placeholder(entity_type: str, index: int, use_faker: bool) -> str:
        result = f"[REDACTED_{entity_type}_{index}]"
        if use_faker:
            result = get_fake_value(entity_type) or result

        return result

    @staticmethod
    def _anonymize(
        prompt: str, pii_entities: List[PIIEntity], vault: Vault, use_faker: bool
    ) -> (str, List[tuple]):
        """
        Replace detected entities in the prompt with anonymized placeholders.

        Parameters:
            prompt (str): Original text prompt.
            pii_entities (List[PIIEntity]): List of entities detected in the prompt.
            vault (Vault): A vault instance with the anonymized data stored.
            use_faker (bool): Whether to use faker to generate fake data.

        Returns:
            str: Sanitized text.
            List[tuple]: List of tuples representing the replaced entities and their corresponding placeholders.
        """
        text_replace_builder = TextReplaceBuilder(original_text=prompt)

        entity_type_counter, new_entity_counter = {}, {}
        for pii_entity in pii_entities:
            entity_type = pii_entity.entity_type
            entity_value = text_replace_builder.get_text_in_position(
                pii_entity.start, pii_entity.end
            )

            if entity_type not in entity_type_counter:
                entity_type_counter[entity_type] = {}

            if entity_value not in entity_type_counter[entity_type]:
                vault_entities = [
                    (entity_placeholder, entity_vault_value)
                    for entity_placeholder, entity_vault_value in vault.get()
                    if entity_type in entity_placeholder
                ]
                entity_placeholder = [
                    entity_placeholder
                    for entity_placeholder, entity_vault_value in vault_entities
                    if entity_vault_value == entity_value
                ]
                if len(entity_placeholder) > 0:
                    entity_type_counter[entity_type][entity_value] = int(
                        entity_placeholder[0].split("_")[-1][:-1]
                    )
                else:
                    entity_type_counter[entity_type][entity_value] = (
                        len(vault_entities) + new_entity_counter.get(entity_type, 0) + 1
                    )
                    new_entity_counter[entity_type] = new_entity_counter.get(entity_type, 0) + 1

        results = []
        sorted_pii_entities = sorted(pii_entities, reverse=True)
        for pii_entity in sorted_pii_entities:
            entity_type = pii_entity.entity_type
            entity_value = text_replace_builder.get_text_in_position(
                pii_entity.start, pii_entity.end
            )

            index = entity_type_counter[entity_type][entity_value]
            changed_entity = Anonymize._get_entity_placeholder(entity_type, index, use_faker)

            results.append((changed_entity, prompt[pii_entity.start : pii_entity.end]))

            text_replace_builder.replace_text_get_insertion_index(
                changed_entity, pii_entity.start, pii_entity.end
            )

        return text_replace_builder.output_text, results

    @staticmethod
    def remove_single_quotes(text: str) -> str:
        text_without_single_quotes = text.replace("'", " ")
        return text_without_single_quotes

    def scan(self, prompt: str) -> (str, bool, float):
        risk_score = 0.0
        if prompt.strip() == "":
            return prompt, True, risk_score

        analyzer_results = self._analyzer.analyze(
            text=Anonymize.remove_single_quotes(prompt),
            language=self._language,
            entities=self._entity_types,
            allow_list=self._allowed_names,
            score_threshold=self._threshold,
        )

        risk_score = round(
            max(analyzer_result.score for analyzer_result in analyzer_results)
            if analyzer_results
            else 0.0,
            1,
        )
        analyzer_results = self._remove_conflicts_and_get_text_manipulation_data(analyzer_results)
        merged_results = self._merge_entities_with_whitespace_between(prompt, analyzer_results)

        sanitized_prompt, anonymized_results = self._anonymize(
            prompt, merged_results, self._vault, self._use_faker
        )

        if prompt != sanitized_prompt:
            LOGGER.warning(
                "Found sensitive data in the prompt and replaced it",
                merged_results=merged_results,
                risk_score=risk_score,
            )
            for entity_placeholder, entity_value in anonymized_results:
                if not self._vault.placeholder_exists(entity_placeholder):
                    self._vault.append((entity_placeholder, entity_value))
            return self._preamble + sanitized_prompt, False, risk_score

        LOGGER.debug("Prompt does not have sensitive data to replace", risk_score=risk_score)

        return prompt, True, 0.0

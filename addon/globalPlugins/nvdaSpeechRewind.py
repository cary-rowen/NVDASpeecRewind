# Copyright (C) 2026 Cary-rowen <cary-rowen@outlook.com>
# This file is covered by the GNU General Public License.
# See the file COPYING.txt for more details.

"""Provide speech history for NVDA."""

from __future__ import annotations

from collections.abc import Callable
from functools import cached_property
from html import escape
from typing import TYPE_CHECKING, Literal

import addonHandler
import api
import braille
import config
import globalCommands
import globalPluginHandler
import inputCore
import speech
import ui
import wx
from config.configSections import registerSection
from gui import guiHelper, nvdaControls
from gui.settingsDialogs import NVDASettingsDialog, SettingsPanel
from scriptHandler import getCurrentScript, getLastScriptRepeatCount, script
from speech.commands import CallbackCommand, _CancellableSpeechCommand
from speech.speech import getSingleCharDescription
from speech.types import SpeechSequence
from textUtils.uniscribe import splitAtCharacterBoundaries

addonHandler.initTranslation()

if TYPE_CHECKING:
	from characterProcessing import SymbolLevel


_CONFIG_SECTION = "NVDASpeechRewind"
_CONFIG_SPEC = {
	"enabled": "boolean(default=false)",
	"maxSize": "integer(default=1000, min=2, max=100000)",
}

# Translators: The name of the NVDA Speech Rewind add-on.
_ADDON_NAME = _("Speech Rewind")


class SpeechHistoryItem:
	"""Store a speech sequence and its symbol level for later replay."""

	def __init__(self, sequence: SpeechSequence, symbolLevel: "SymbolLevel | None") -> None:
		self.sequence = [
			item for item in sequence if not isinstance(item, (CallbackCommand, _CancellableSpeechCommand))
		]
		if not any(isinstance(item, str) for item in self.sequence):
			raise ValueError("The speech sequence does not contain any speech")
		self.symbolLevel = symbolLevel

	@property
	def text(self) -> str:
		"""Return the text chunks in this item, separated as NVDA normally separates them."""
		return speech.CHUNK_SEPARATOR.join(item for item in self.sequence if isinstance(item, str))

	@cached_property
	def characters(self) -> tuple[str, ...]:
		"""Return the visible characters in this item using NVDA's character boundaries."""
		return tuple(splitAtCharacterBoundaries(self.text))


class SpeechHistoryBuffer:
	"""Keep recent speech items and the item currently selected for review."""

	def __init__(self, ignoredScripts: set[Callable[..., None]]) -> None:
		self.items: list[SpeechHistoryItem] = []
		self.reviewPosition: int | None = None
		self.characterPosition = 0
		self.ignoredScripts = ignoredScripts

	def addSequence(
		self,
		speechSequence: SpeechSequence,
		originalSpeechSequence: SpeechSequence | None = None,
		symbolLevel: "SymbolLevel | None" = None,
	) -> None:
		"""Add a prepared speech sequence unless it was produced by a history command."""
		if getCurrentScript() in self.ignoredScripts:
			return
		# NVDA 2026.3 does not provide originalSpeechSequence to this hook.
		historySequence = originalSpeechSequence if originalSpeechSequence is not None else speechSequence
		try:
			item = SpeechHistoryItem(historySequence, symbolLevel)
		except ValueError:
			return
		maxSize = config.conf[_CONFIG_SECTION]["maxSize"] if config.conf[_CONFIG_SECTION]["enabled"] else 1
		self.items.append(item)
		self.trimToSize(maxSize)
		self.reviewPosition = len(self.items) - 1
		self.characterPosition = 0

	def trimToSize(self, maxSize: int) -> None:
		excess = len(self.items) - maxSize
		if excess <= 0:
			return
		del self.items[:excess]
		if self.reviewPosition is not None:
			if self.reviewPosition < excess:
				self.reviewPosition = 0
				self.characterPosition = 0
			else:
				self.reviewPosition -= excess

	def clearSpeechHistory(self, reportConfirmation: bool = False) -> None:
		"""Clear all items and optionally report the action to the user."""
		self.items.clear()
		self.reviewPosition = None
		self.characterPosition = 0
		if reportConfirmation:
			# Translators: Message reported after speech history is cleared.
			ui.message(_("Speech history cleared"))

	def reviewPrevious(self) -> SpeechHistoryItem:
		"""Select and return the item before the current review position."""
		if self.reviewPosition is None or self.reviewPosition == 0:
			raise LookupError()
		self.reviewPosition -= 1
		self.characterPosition = 0
		return self.currentReviewItem()

	def reviewNext(self) -> SpeechHistoryItem:
		"""Select and return the item after the current review position."""
		if self.reviewPosition is None or self.reviewPosition == len(self.items) - 1:
			raise LookupError()
		self.reviewPosition += 1
		self.characterPosition = 0
		return self.currentReviewItem()

	def currentReviewItem(self) -> SpeechHistoryItem:
		"""Return the selected item or raise ``LookupError`` when none is selected."""
		if self.reviewPosition is None:
			raise LookupError()
		return self.items[self.reviewPosition]

	def _currentCharacters(self) -> tuple[str, ...]:
		characters = self.currentReviewItem().characters
		if not characters:
			raise LookupError()
		return characters

	def currentReviewCharacter(self) -> str:
		"""Return the character currently selected for review."""
		return self._currentCharacters()[self.characterPosition]

	def _reviewCharacter(self, direction: Literal[-1, 1]) -> tuple[str, bool]:
		"""Move one character and return it with whether the edge was reached."""
		characters = self._currentCharacters()
		boundaryPosition = 0 if direction < 0 else len(characters) - 1
		atBoundary = self.characterPosition == boundaryPosition
		if not atBoundary:
			self.characterPosition += direction
		return characters[self.characterPosition], atBoundary

	def reviewPreviousCharacter(self) -> tuple[str, bool]:
		"""Select the previous character and return it with the boundary state."""
		return self._reviewCharacter(-1)

	def reviewNextCharacter(self) -> tuple[str, bool]:
		"""Select the next character and return it with the boundary state."""
		return self._reviewCharacter(1)


class SpeechHistorySettingsPanel(SettingsPanel):
	"""Configure speech history from the standard NVDA Settings dialog."""

	# The settings category uses the add-on name so it is easy to find.
	title = _ADDON_NAME
	historyBuffer: SpeechHistoryBuffer | None = None

	def makeSettings(self, sizer: wx.BoxSizer) -> None:
		helper = guiHelper.BoxSizerHelper(self, sizer=sizer)
		self.enabledCheckbox: wx.CheckBox = helper.addItem(
			wx.CheckBox(
				self,
				# Translators: Checkbox to enable recording speech history.
				label=_("Enable speech history"),
			),
		)
		self.enabledCheckbox.SetValue(config.conf[_CONFIG_SECTION]["enabled"])
		self.enabledCheckbox.Bind(wx.EVT_CHECKBOX, self._onEnabledChange)

		validation = config.conf.getConfigValidation((_CONFIG_SECTION, "maxSize"))
		self.maxSizeEdit: nvdaControls.SelectOnFocusSpinCtrl = helper.addLabeledControl(
			# Translators: Label for the maximum number of speech history items.
			_("Maximum number of speech history items:"),
			nvdaControls.SelectOnFocusSpinCtrl,
			min=int(validation.kwargs["min"]),
			max=int(validation.kwargs["max"]),
			initial=config.conf[_CONFIG_SECTION]["maxSize"],
		)

		self.clearButton: wx.Button = helper.addItem(
			wx.Button(
				self,
				# Translators: Button to clear all speech history items.
				label=_("Clear speech history"),
			),
		)
		self.clearButton.Bind(wx.EVT_BUTTON, self._onClear)
		self._updateEnabledState()

	def _updateEnabledState(self) -> None:
		enabled = self.enabledCheckbox.IsChecked()
		self.maxSizeEdit.Enable(enabled)
		self.clearButton.Enable(enabled)

	def _onEnabledChange(self, evt: wx.CommandEvent) -> None:
		self._updateEnabledState()
		evt.Skip()

	def _onClear(self, evt: wx.CommandEvent) -> None:
		self._getHistoryBuffer().clearSpeechHistory(reportConfirmation=True)

	def _getHistoryBuffer(self) -> SpeechHistoryBuffer:
		if self.historyBuffer is None:
			raise RuntimeError("Speech history buffer is not initialized")
		return self.historyBuffer

	def onSave(self) -> None:
		enabled = self.enabledCheckbox.IsChecked()
		config.conf[_CONFIG_SECTION]["enabled"] = enabled
		config.conf[_CONFIG_SECTION]["maxSize"] = self.maxSizeEdit.GetValue()
		historyBuffer = self._getHistoryBuffer()
		if not enabled:
			historyBuffer.clearSpeechHistory()
		else:
			historyBuffer.trimToSize(config.conf[_CONFIG_SECTION]["maxSize"])


class GlobalPlugin(globalPluginHandler.GlobalPlugin):
	"""Collect and expose recent NVDA speech through global commands."""

	def __init__(self) -> None:
		"""Initialize the buffer, configuration section, speech hook and settings panel."""
		super().__init__()
		if _CONFIG_SECTION not in config.conf.spec:
			registerSection(_CONFIG_SECTION, _CONFIG_SPEC)
			# Add-ons load after config initialization, so register the live spec as well.
			config.conf.spec[_CONFIG_SECTION] = _CONFIG_SPEC
		ignoredScripts = {
			type(self).script_repeatLastSpokenInformation,
			type(self).script_reviewPreviousItemInSpeechHistory,
			type(self).script_reviewNextItemInSpeechHistory,
			type(self).script_copyCurrentlyReviewedSpeechHistoryItem,
			type(self).script_reviewPreviousCharacterInSpeechHistory,
			type(self).script_reviewCurrentCharacterInSpeechHistory,
			type(self).script_reviewNextCharacterInSpeechHistory,
		}
		self.buffer = SpeechHistoryBuffer(ignoredScripts)
		speech.pre_speech.register(self.buffer.addSequence)
		SpeechHistorySettingsPanel.historyBuffer = self.buffer
		if SpeechHistorySettingsPanel not in NVDASettingsDialog.categoryClasses:
			NVDASettingsDialog.categoryClasses.append(SpeechHistorySettingsPanel)

	def terminate(self) -> None:
		"""Remove the speech hook and the settings category."""
		speech.pre_speech.unregister(self.buffer.addSequence)
		if SpeechHistorySettingsPanel in NVDASettingsDialog.categoryClasses:
			NVDASettingsDialog.categoryClasses.remove(SpeechHistorySettingsPanel)
		SpeechHistorySettingsPanel.historyBuffer = None

	@script(
		description=_(
			# Translators: Description for the command that repeats the current speech history item.
			"Reports the last spoken information, or the current item when reviewing speech history. "
			"Pressing twice shows the whole speech history in a browsable message.",
		),
		gesture="kb:NVDA+x",
		category=globalCommands.SCRCAT_SPEECH,
		speakOnDemand=True,
	)
	def script_repeatLastSpokenInformation(self, gesture: inputCore.InputGesture) -> None:
		try:
			item = self.buffer.currentReviewItem()
		except LookupError:
			return
		repeats = getLastScriptRepeatCount()
		if repeats == 0:
			speech.speak(item.sequence, symbolLevel=item.symbolLevel)
			braille.handler.message(item.text)
		elif repeats == 1:
			content = "<hr>".join(
				f"<p>{escape(historyItem.text)}</p>" for historyItem in reversed(self.buffer.items)
			)
			ui.browseableMessage(
				content,
				# Translators: Title of the window containing NVDA Speech Rewind history.
				_ADDON_NAME,
				isHtml=True,
				copyButton=True,
				closeButton=True,
			)

	@script(
		# Translators: Description for the command to review the previous speech history item.
		description=_("Review the previous item in speech history"),
		category=globalCommands.SCRCAT_SPEECH,
	)
	def script_reviewPreviousItemInSpeechHistory(self, gesture: inputCore.InputGesture) -> None:
		try:
			item = self.buffer.reviewPrevious()
		except LookupError:
			# Translators: Message reported when there is no older speech history item.
			ui.message(_("No previous item"))
			return
		ui.message(item.text)

	@script(
		# Translators: Description for the command to review the next speech history item.
		description=_("Review the next item in speech history"),
		category=globalCommands.SCRCAT_SPEECH,
	)
	def script_reviewNextItemInSpeechHistory(self, gesture: inputCore.InputGesture) -> None:
		try:
			item = self.buffer.reviewNext()
		except LookupError:
			# Translators: Message reported when there is no newer speech history item.
			ui.message(_("No next item"))
			return
		ui.message(item.text)

	@script(
		# Translators: Description for the command to copy the current speech history item.
		description=_(
			"Copy the last spoken information to the clipboard, or the current item when reviewing speech history."
		),
		gesture="kb:NVDA+control+x",
		category=globalCommands.SCRCAT_SPEECH,
	)
	def script_copyCurrentlyReviewedSpeechHistoryItem(self, gesture: inputCore.InputGesture) -> None:
		try:
			item = self.buffer.currentReviewItem()
		except LookupError:
			# Translators: Message reported when there is no speech history item to copy.
			ui.message(_("Nothing to copy"))
			return
		api.copyToClip(item.text.strip(), notify=True)

	def _getCurrentHistoryCharacter(self) -> str | None:
		try:
			return self.buffer.currentReviewCharacter()
		except LookupError:
			self._reportNoHistoryItem()
			return None

	def _reportNoHistoryItem(self) -> None:
		# Translators: Message reported when there is no speech history item to review by character.
		ui.message(_("No speech history item"))

	def _speakReviewedCharacter(
		self,
		reviewCharacter: Callable[[], tuple[str, bool]],
		boundaryMessage: str,
	) -> None:
		"""Move to and speak a character, reporting when an edge is reached."""
		try:
			character, atBoundary = reviewCharacter()
		except LookupError:
			self._reportNoHistoryItem()
			return
		if atBoundary:
			ui.reviewMessage(boundaryMessage)
		speech.speakSpelling(character)
		if config.conf["speech"]["delayedCharacterDescriptions"]:
			speech.speak(list(getSingleCharDescription(character)))

	@script(
		description=_(
			# Translators: Input help mode message for moving to the previous character in speech history.
			"Moves to the previous character in speech history and speaks it",
		),
		category=globalCommands.SCRCAT_SPEECH,
	)
	def script_reviewPreviousCharacterInSpeechHistory(self, gesture: inputCore.InputGesture) -> None:
		# Translators: Message reported when reviewing at the left edge of text.
		self._speakReviewedCharacter(self.buffer.reviewPreviousCharacter, _("Left"))

	@script(
		description=_(
			# Translators: Input help mode message for reporting the current character in speech history.
			"Reports the current character in speech history. "
			"Pressing twice reports a description or example of that character. "
			"Pressing three times reports the numeric value of the character in decimal and hexadecimal",
		),
		category=globalCommands.SCRCAT_SPEECH,
		speakOnDemand=True,
	)
	def script_reviewCurrentCharacterInSpeechHistory(self, gesture: inputCore.InputGesture) -> None:
		character = self._getCurrentHistoryCharacter()
		if character is None:
			return
		scriptCount = getLastScriptRepeatCount()
		if scriptCount == 0:
			speech.speakSpelling(character)
		elif scriptCount == 1:
			speech.speakSpelling(character, useCharacterDescriptions=True)
		else:
			characterValues = [ord(c) for c in character]
			for value in characterValues:
				speech.speakMessage(f"{value},")
				if len(characterValues) == 1:
					speech.speakSpelling(hex(value))
			braille.handler.message("; ".join(f"{value}, {hex(value)}" for value in characterValues))

	@script(
		description=_(
			# Translators: Input help mode message for moving to the next character in speech history.
			"Moves to the next character in speech history and speaks it",
		),
		category=globalCommands.SCRCAT_SPEECH,
	)
	def script_reviewNextCharacterInSpeechHistory(self, gesture: inputCore.InputGesture) -> None:
		# Translators: Message reported when reviewing at the right edge of text.
		self._speakReviewedCharacter(self.buffer.reviewNextCharacter, _("Right"))

document.addEventListener('DOMContentLoaded', () => {
	const token = document.querySelector('meta[name="csrf-token"]')?.content;
	if (token) {
		document.querySelectorAll('form[method="post"], form[method="POST"]').forEach((form) => {
			if (form.querySelector('input[name="_csrf_token"]')) return;
			const input = document.createElement('input');
			input.type = 'hidden';
			input.name = '_csrf_token';
			input.value = token;
			form.appendChild(input);
		});
	}
	const helpDialog = document.getElementById('help-dialog');
	const helpSearch = document.getElementById('help-search');
	const helpStatus = document.getElementById('help-search-status');
	const helpNoResults = document.getElementById('help-no-results');
	const helpModules = [...document.querySelectorAll('.help-module')];
	const helpTopics = [...document.querySelectorAll('[data-help-item]')];
	const normalizeHelpText = (value) => value.normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLowerCase().trim();
	const articleFilter = document.getElementById('filtro_artigos');
	const articleOptions = document.querySelectorAll('#caixa_artigos .article-option');
	articleFilter?.addEventListener('input', () => {
		const term = normalizeHelpText(articleFilter.value);
		articleOptions.forEach((option) => {
			option.hidden = !normalizeHelpText(option.textContent).includes(term);
		});
	});
	document.querySelectorAll('[data-help-open]').forEach((button) => {
		button.addEventListener('click', () => {
			helpDialog?.showModal();
			helpSearch?.focus();
		});
	});
	document.querySelectorAll('[data-help-close]').forEach((button) => {
		button.addEventListener('click', () => helpDialog?.close());
	});
	helpDialog?.addEventListener('keydown', (event) => {
		if (event.key === 'Escape') {
			event.preventDefault();
			helpDialog.close();
		}
	});
	helpDialog?.addEventListener('click', (event) => {
		if (event.target === helpDialog) helpDialog.close();
	});
	helpSearch?.addEventListener('input', () => {
		const terms = normalizeHelpText(helpSearch.value).split(/\s+/).filter(Boolean);
		let visibleTopics = 0;
		helpModules.forEach((module) => {
			const moduleTopics = [...module.querySelectorAll('[data-help-item]')];
			let moduleMatches = 0;
			moduleTopics.forEach((topic) => {
				const text = normalizeHelpText(topic.textContent);
				const matches = terms.every((term) => text.includes(term));
				topic.hidden = !matches;
				if (matches) moduleMatches += 1;
			});
			module.hidden = moduleMatches === 0;
			if (terms.length && moduleMatches) module.open = true;
			visibleTopics += moduleMatches;
		});
		if (helpStatus) helpStatus.textContent = terms.length
			? `${visibleTopics} tópico(s) encontrado(s)`
			: `${helpModules.length} módulos disponíveis`;
		if (helpNoResults) helpNoResults.hidden = visibleTopics > 0;
	});
	document.addEventListener('keydown', (event) => {
		if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k' && helpDialog) {
			event.preventDefault();
			if (!helpDialog.open) helpDialog.showModal();
			helpSearch?.focus();
		}
	});
	const selectTema = document.getElementById('select-tema-grupo');
	const assistidosItems = document.querySelectorAll('.assistido-item');
	if (selectTema && assistidosItems.length > 0) {
		selectTema.addEventListener('change', function () {
			const temaSelecionado = this.value.toLowerCase().trim();
			assistidosItems.forEach((item) => {
				const temaAssistido = (item.getAttribute('data-tema') || '').toLowerCase().trim();
				if (temaSelecionado === '' || temaSelecionado.includes('todos os tipos')) {
					item.style.display = '';
				} else if (temaAssistido === temaSelecionado || temaAssistido.includes(temaSelecionado)) {
					item.style.display = '';
				} else {
					item.style.display = 'none';
				}
			});
		});
	}
	const documentFeedback = document.querySelector('[data-document-feedback]');
	const showDocumentFeedback = (message, isError = false) => {
		if (!documentFeedback) return;
		documentFeedback.textContent = message;
		documentFeedback.hidden = false;
		documentFeedback.classList.toggle('is-error', isError);
	};
	document.querySelectorAll('[data-document-upload]').forEach((form) => {
		const fileInput = form.querySelector('[data-upload-file]');
		form.querySelector('[data-upload-trigger]')?.addEventListener('click', () => fileInput?.click());
		fileInput?.addEventListener('change', async () => {
			if (!fileInput.files?.length) return;
			const buttons = form.querySelectorAll('button');
			buttons.forEach((button) => { button.disabled = true; });
			try {
				const response = await fetch(form.action, {
					method: 'POST',
					headers: { Accept: 'application/json' },
					body: new FormData(form),
				});
				const result = await response.json().catch(() => ({}));
				if (!response.ok) throw new Error(result.error || 'Não foi possível anexar o documento.');
				showDocumentFeedback('Documento anexado com sucesso. Atualizando a lista...');
				window.setTimeout(() => window.location.reload(), 700);
			} catch (error) {
				showDocumentFeedback(error.message || 'Não foi possível anexar o documento.', true);
				buttons.forEach((button) => { button.disabled = false; });
				fileInput.value = '';
			}
		});
	});
	document.querySelectorAll('[data-delete-url][data-filename]').forEach((button) => {
		button.addEventListener('click', async () => {
			if (!window.confirm('Deseja realmente excluir este arquivo?')) return;
			button.disabled = true;
			try {
				const response = await fetch(button.dataset.deleteUrl, {
					method: 'POST',
					headers: { Accept: 'application/json' },
					body: new URLSearchParams({ filename: button.dataset.filename, _csrf_token: token || '' }),
				});
				const result = await response.json().catch(() => ({}));
				if (!response.ok) throw new Error(result.error || 'Não foi possível excluir o arquivo.');
				const list = button.closest('.document-attachment-files');
				button.closest('li')?.remove();
				if (list && !list.children.length) {
					const message = list.parentElement.querySelector('.document-empty-message');
					list.hidden = true;
					if (message) message.hidden = false;
				}
				showDocumentFeedback('Documento excluído com sucesso.');
			} catch (error) {
				showDocumentFeedback(error.message || 'Não foi possível excluir o arquivo.', true);
				button.disabled = false;
			}
		});
	});
	document.querySelectorAll('[data-remove-group-participants]').forEach((form) => {
		form.addEventListener('submit', async (event) => {
			event.preventDefault();
			const selected = [...form.querySelectorAll('input[name="pessoa_id"]:checked')];
			const feedback = form.closest('.group-participant-removal').querySelector('[data-group-participant-feedback]');
			const showFeedback = (message, isError = false) => {
				feedback.textContent = message;
				feedback.hidden = false;
				feedback.classList.toggle('is-error', isError);
			};
			if (!selected.length) {
				showFeedback('Selecione ao menos um participante para remover.', true);
				return;
			}
			if (!window.confirm(`Deseja remover ${selected.length} participante(s) deste grupo?`)) return;
			const submitButton = form.querySelector('button[type="submit"]');
			submitButton.disabled = true;
			try {
				const response = await fetch(form.action, {
					method: 'POST',
					headers: { Accept: 'application/json' },
					body: new FormData(form),
				});
				const result = await response.json().catch(() => ({}));
				if (!response.ok) throw new Error(result.error || 'Não foi possível remover os participantes.');
				const groupCard = form.closest('[data-group-card]');
				const removedIds = new Set(result.removed_ids.map(String));
				removedIds.forEach((personId) => {
					groupCard.querySelectorAll(`[data-person-id="${personId}"]`).forEach((element) => element.remove());
				});
				const count = groupCard.querySelector('[data-group-participant-count]');
				const remaining = Number(count.textContent) - result.removed_count;
				count.textContent = remaining;
				if (remaining === 0) {
					const emptyRow = groupCard.querySelector('.group-attendance-form table').insertRow();
					const emptyCell = emptyRow.insertCell();
					emptyCell.colSpan = 4;
					emptyCell.textContent = 'Este grupo ainda não possui participantes.';
				}
				const frequencyForm = groupCard.querySelector('.group-attendance-form');
				if (frequencyForm) {
					const records = JSON.parse(frequencyForm.dataset.frequencies);
					removedIds.forEach((personId) => { delete records[personId]; });
					frequencyForm.dataset.frequencies = JSON.stringify(records);
				}
				if (remaining === 0) {
					const message = document.createElement('p');
					message.className = 'muted';
					message.dataset.noGroupParticipants = '';
					message.textContent = 'Este grupo ainda não possui participantes.';
					form.querySelector('.group-participant-list').replaceChildren(message);
				}
				showFeedback(result.message);
			} catch (error) {
				showFeedback(error.message || 'Não foi possível remover os participantes.', true);
			} finally {
				submitButton.disabled = false;
			}
		});
	});
	document.querySelectorAll('[data-message-delete][data-delete-url]').forEach((button) => {
		button.addEventListener('click', async () => {
			if (!window.confirm('Deseja excluir permanentemente este comunicado?')) return;
			button.disabled = true;
			try {
				const response = await fetch(button.dataset.deleteUrl, {
					method: 'DELETE',
					headers: { Accept: 'application/json', 'X-CSRF-Token': token || '' },
				});
				if (!response.ok) throw new Error('Não foi possível excluir o comunicado.');
				button.closest('.message-item')?.remove();
			} catch (error) {
				window.alert(error.message || 'Não foi possível excluir o comunicado.');
				button.disabled = false;
			}
		});
	});
});
const chartField = document.querySelector('#chart-field');
const dashboardChart = document.querySelector('#dashboard-chart');

function renderDashboardChart() {
	if (!chartField || !dashboardChart) return;
	const values = window.dashboardCharts[chartField.value] || [];
	if (!values.length) {
		dashboardChart.innerHTML = '<p class="muted">Ainda não há dados para esta modalidade.</p>';
		return;
	}
	const maximum = Math.max(...values.map((item) => item[1]));
	dashboardChart.replaceChildren(...values.map(([label, total]) => {
		const percentage = Math.max(4, Math.round((total / maximum) * 100));
		const row = document.createElement('div');
		row.className = 'bar-row';
		const labelLine = document.createElement('div');
		labelLine.className = 'bar-label';
		const labelText = document.createElement('span');
		labelText.textContent = label;
		const totalText = document.createElement('strong');
		totalText.textContent = total;
		labelLine.append(labelText, totalText);
		const track = document.createElement('div');
		track.className = 'bar-track';
		const fill = document.createElement('span');
		fill.style.width = `${percentage}%`;
		track.append(fill);
		row.append(labelLine, track);
		return row;
	}));
}

chartField?.addEventListener('change', renderDashboardChart);
renderDashboardChart();

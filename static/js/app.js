document.addEventListener('DOMContentLoaded', () => {
	const token = document.querySelector('meta[name="csrf-token"]')?.content;
	if (!token) return;
	document.querySelectorAll('form[method="post"], form[method="POST"]').forEach((form) => {
		if (form.querySelector('input[name="_csrf_token"]')) return;
		const input = document.createElement('input');
		input.type = 'hidden';
		input.name = '_csrf_token';
		input.value = token;
		form.appendChild(input);
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

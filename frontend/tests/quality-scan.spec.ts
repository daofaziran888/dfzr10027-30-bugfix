import { expect, test } from '@playwright/test'

/**
 * 质量扫描分母修复的端到端验证：
 * 1. 实测 5000 行 / 12 条异常 → 0.24% 异常率、76 分、低于 80 分触发复核；
 * 2. 缺分母旧规则只显示「缺分母」，不进异常率；
 * 3. 交班日报区分订单级与整批覆盖口径，可转交订单带实测依据；
 * 4. 补扫后评分刷新到 80，历史转交记录仍保留 76 分 / 5000 行的原始快照。
 */
test.describe('质量扫描实测分母与交班日报', () => {
  test.beforeEach(async ({ request }) => {
    // 重置内存后端到种子状态，再灌入初始两批样本（一条实测 + 一条缺分母摘要）。
    await request.post('/api/test/reset')
    await request.post('/api/pipeline/quality-scan')
  })

  test('扫描结果按实测行数显示异常率与评分', async ({ page }) => {
    await page.goto('/quality_mon')
    const panel = page.getByTestId('quality-scan-panel')
    await expect(panel).toBeVisible()

    await page.getByTestId('run-quality-scan').click()

    await expect(page.getByTestId('stat-score').locator('.stat-value')).toHaveText('76')
    await expect(panel.getByText('0.24%').first()).toBeVisible()
    await expect(panel.getByText('5000').first()).toBeVisible()

    const rows = page.getByTestId('rule-result-table').locator('tbody tr')
    await expect(rows).toHaveCount(2)
    await expect(rows.filter({ hasText: 'QUAL-COMPLETENESS' })).toContainText('实测有效')
    await expect(rows.filter({ hasText: 'QUAL-ACCURACY' })).toContainText('缺分母')
  })

  test('日报给出复核结论并区分订单与整批覆盖范围', async ({ page }) => {
    await page.goto('/quality_mon')
    await page.getByTestId('run-quality-scan').click()

    const report = page.getByTestId('daily-report')
    await expect(report).toContainText('76 分低于 80 分，触发人工复核')
    await expect(report).toContainText('仅覆盖已实测规则命中的异常订单，不是整批数据结论')
    await expect(report).toContainText('QUAL-ACCURACY')

    const transferable = page.getByTestId('transferable-table').locator('tbody tr')
    await expect(transferable).toHaveCount(12)
    await expect(transferable.first()).toContainText('扫描 5000 行发现 12 条异常')
  })

  test('转交后补扫，评分刷新且历史转交依据保留', async ({ page }) => {
    await page.goto('/quality_mon')
    await page.getByTestId('run-quality-scan').click()

    await page.getByTestId('transferable-table').getByTestId('transfer-order').first().click()

    const transferRows = page.getByTestId('transfer-table').locator('tbody tr')
    await expect(transferRows).toHaveCount(1)
    await expect(transferRows.first()).toContainText('76')
    await expect(transferRows.first()).toContainText('0.24%')
    await expect(transferRows.first()).toContainText('5000')

    await page.getByTestId('run-quality-rescan').click()

    await expect(page.getByTestId('stat-score').locator('.stat-value')).toHaveText('80')
    const report = page.getByTestId('daily-report')
    await expect(report).toContainText('全部规则均已实测，结论覆盖本班全部规则范围')

    // 补扫后没有新的可转交订单（评分达到门槛），但历史转交记录仍在。
    await expect(page.getByTestId('transferable-table').locator('.empty-state')).toBeVisible()
    await expect(transferRows).toHaveCount(1)
    await expect(transferRows.first()).toContainText('76')
    await expect(transferRows.first()).toContainText('5000')
  })

  test('新扫描面板与原有规则 CRUD 共存且无接口错误', async ({ page }) => {
    const errors: string[] = []
    page.on('pageerror', e => errors.push(e.message))
    page.on('response', r => { if (r.url().includes('/api/') && r.status() >= 400) errors.push(`${r.status()} ${r.url()}`) })

    await page.goto('/quality_mon')
    // 原有规则表（seed 4 行）与新面板同时渲染。
    await expect(page.locator('tbody tr[data-testid^="row-"]')).toHaveCount(4)
    await expect(page.getByTestId('quality-scan-panel')).toBeVisible()
    await page.getByTestId('run-quality-scan').click()
    // 重置后只应有一条实测 + 一条缺分母，共 2 条批次×规则结果。
    await expect(page.getByTestId('rule-result-table').locator('tbody tr')).toHaveCount(2)
    // 原有动作按钮仍可正常流转。
    await page.locator('[data-testid^="action-1-"]').first().click()
    await expect(page.locator('tbody tr[data-testid="row-1"]')).toHaveAttribute('data-status', '已启用')
    expect(errors).toEqual([])
  })
})

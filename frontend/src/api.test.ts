import { createApi, readBootstrapFragment } from './api'

describe('bootstrap and API security behavior', () => {
  test('reads and synchronously removes the one-time bootstrap fragment', () => {
    window.history.replaceState(null, '', '/process/?safe=yes#bootstrap=one-time-value')

    const token = readBootstrapFragment()

    expect(token).toBe('one-time-value')
    expect(window.location.hash).toBe('')
    expect(window.location.pathname).toBe('/process/')
    expect(window.location.search).toBe('?safe=yes')
  })

  test('rejects ambiguous bootstrap fragments and removes all fragment material', () => {
    window.history.replaceState(null, '', '/process/#bootstrap=one&bootstrap=two&other=data')

    expect(readBootstrapFragment()).toBeNull()
    expect(window.location.hash).toBe('')
  })

  test('exchanges bootstrap in Authorization and protects mutations with in-memory CSRF', async () => {
    window.history.replaceState(null, '', '/route/')
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(new Response(JSON.stringify({ csrf: 'csrf-value' }), { status: 200, headers: { 'Content-Type': 'application/json' } }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ mode: 'managed', report: null }), { status: 200, headers: { 'Content-Type': 'application/json' } }))
    const api = createApi(fetcher)

    await api.exchangeBootstrap('bootstrap-value')
    await api.selectMode('managed', 8)

    const first = fetcher.mock.calls[0]
    const second = fetcher.mock.calls[1]
    expect(first?.[0]).toBe('/route/api/bootstrap')
    expect(first?.[0]).not.toContain('bootstrap-value')
    expect(new Headers(first?.[1]?.headers).get('Authorization')).toBe('Bearer bootstrap-value')
    expect(new Headers(second?.[1]?.headers).get('X-CSRF-Token')).toBe('csrf-value')
    expect(second?.[1]?.credentials).toBe('same-origin')
    expect(second?.[1]?.cache).toBe('no-store')
  })

  test('binds catalog mutations to an explicit snapshot even after later status reads', async () => {
    window.history.replaceState(null, '', '/route/')
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(new Response(JSON.stringify({ csrf: 'csrf-value' }), { status: 200, headers: { 'Content-Type': 'application/json' } }))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        mode: 'legacy',
        selected_catalog: '/private/catalog.sqlite3',
        bootstrap_revision: 4,
        report: { catalog_revision: 7 },
      }), { status: 200, headers: { 'Content-Type': 'application/json' } }))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        mode: 'legacy',
        selected_catalog: '/private/other.sqlite3',
        bootstrap_revision: 5,
        report: { catalog_revision: 7 },
      }), { status: 200, headers: { 'Content-Type': 'application/json' } }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ status: 'active' }), { status: 200, headers: { 'Content-Type': 'application/json' } }))
    const api = createApi(fetcher)
    await api.session()
    await api.status()
    const reviewedTarget = { expected_bootstrap_revision: 4, expected_catalog: '/private/catalog.sqlite3' }
    await api.status()

    await api.activateCatalog(7, reviewedTarget)

    const mutationBody = fetcher.mock.calls[3]?.[1]?.body
    expect(typeof mutationBody).toBe('string')
    if (typeof mutationBody !== 'string') throw new Error('Expected a JSON request body')
    expect(JSON.parse(mutationBody)).toEqual({
      expected_revision: 7,
      expected_bootstrap_revision: 4,
      expected_catalog: '/private/catalog.sqlite3',
    })
  })

  test('serializes policy updates with strict request fields and explicit target', async () => {
    window.history.replaceState(null, '', '/route/')
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(new Response(JSON.stringify({ csrf: 'csrf-value' }), { status: 200, headers: { 'Content-Type': 'application/json' } }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ revision: 9 }), { status: 200, headers: { 'Content-Type': 'application/json' } }))
    const api = createApi(fetcher)
    await api.session()

    await api.updatePolicy({
      revision: 8,
      enable_attachment_download: false,
      allowed_recipients: [],
      allowed_senders: [],
      report_blocked_mutations: false,
    }, { expected_bootstrap_revision: 4, expected_catalog: '/private/catalog.sqlite3' })

    const body = fetcher.mock.calls[1]?.[1]?.body
    expect(typeof body).toBe('string')
    if (typeof body !== 'string') throw new Error('Expected a JSON request body')
    expect(JSON.parse(body)).toEqual({
      expected_revision: 8,
      enable_attachment_download: false,
      allowed_recipients: [],
      allowed_senders: [],
      report_blocked_mutations: false,
      expected_bootstrap_revision: 4,
      expected_catalog: '/private/catalog.sqlite3',
    })
  })

  test('returns a typed conflict without replaying a mutation', async () => {
    window.history.replaceState(null, '', '/route/')
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(new Response(JSON.stringify({ csrf: 'csrf-value' }), { status: 200, headers: { 'Content-Type': 'application/json' } }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ category: 'conflict', message: 'Revision changed.', current: { revision: 9 } }), { status: 409, headers: { 'Content-Type': 'application/json' } }))
    const api = createApi(fetcher)
    await api.session()

    await expect(api.selectMode('managed', 8)).rejects.toEqual(expect.objectContaining({ status: 409, current: { revision: 9 } }))
    expect(fetcher).toHaveBeenCalledTimes(2)
  })
})

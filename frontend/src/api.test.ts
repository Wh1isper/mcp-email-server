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

  test('initializes and selects the default catalog in one mutation', async () => {
    window.history.replaceState(null, '', '/route/')
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(new Response(JSON.stringify({ csrf: 'csrf-value' }), { status: 200, headers: { 'Content-Type': 'application/json' } }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ database: '/private/catalog.sqlite3' }), { status: 200, headers: { 'Content-Type': 'application/json' } }))
    const api = createApi(fetcher)
    await api.session()

    await api.initializeDefaultCatalog(4, true)

    expect(fetcher.mock.calls[1]?.[0]).toBe('/route/api/catalog/initialize-default')
    const mutationBody = fetcher.mock.calls[1]?.[1]?.body
    expect(typeof mutationBody).toBe('string')
    if (typeof mutationBody !== 'string') throw new Error('Expected a JSON request body')
    expect(JSON.parse(mutationBody)).toEqual({
      expected_bootstrap_revision: 4,
      require_empty_install: true,
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
      enable_attachment_content: true,
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
      enable_attachment_content: true,
      allowed_recipients: [],
      allowed_senders: [],
      report_blocked_mutations: false,
      expected_bootstrap_revision: 4,
      expected_catalog: '/private/catalog.sqlite3',
    })
  })

  test('serializes account tags in create and update payloads', async () => {
    window.history.replaceState(null, '', '/route/')
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(new Response(JSON.stringify({ csrf: 'csrf-value' }), { status: 200, headers: { 'Content-Type': 'application/json' } }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ incoming: {}, outgoing: null }), { status: 200, headers: { 'Content-Type': 'application/json' } }))
      .mockResolvedValueOnce(new Response(JSON.stringify({}), { status: 200, headers: { 'Content-Type': 'application/json' } }))
    const api = createApi(fetcher)
    await api.session()
    const input = {
      name: 'work',
      full_name: 'Alice Example',
      email_address: 'alice@example.test',
      save_to_sent: true,
      sent_folder_name: null,
      incoming: {
        host: 'imap.example.test', port: 993, user_name: 'alice@example.test',
        use_ssl: true, start_ssl: false, verify_ssl: true,
      },
      outgoing: null,
      tags: [{ name: 'todo', keyword: '$label4', description: 'Needs action', writable: true }],
    }
    const target = { expected_bootstrap_revision: 4, expected_catalog: '/private/catalog.sqlite3' }

    await api.createAccount(input, { incoming: 'secret', outgoing: null }, 6, target)
    await api.updateAccount('work', { ...input, expected_revision: 7 }, target)

    const createBody = fetcher.mock.calls[1]?.[1]?.body
    const updateBody = fetcher.mock.calls[2]?.[1]?.body
    expect(typeof createBody).toBe('string')
    expect(typeof updateBody).toBe('string')
    if (typeof createBody !== 'string' || typeof updateBody !== 'string') throw new Error('Expected JSON request bodies')
    expect(JSON.parse(createBody)).toEqual(expect.objectContaining({
      tags: input.tags,
      credentials: { incoming: 'secret', outgoing: null },
      expected_catalog_revision: 6,
      ...target,
    }))
    expect(JSON.parse(updateBody)).toEqual(expect.objectContaining({
      tags: input.tags,
      expected_revision: 7,
      ...target,
    }))
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

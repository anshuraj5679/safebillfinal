import { NextRequest, NextResponse } from 'next/server'
import { BackendApiError, backendApiFetch, resolveRequestAuthToken, withQuery } from '@/lib/backend-api'

export const runtime = 'nodejs'
export const maxDuration = 60
const INGEST_TIMEOUT_MS = 120000
const DOCUMENT_FETCH_TIMEOUT_MS = 60000
const DEFAULT_OCR_MODE = 'hybrid'

interface IngestResponse {
  document_id: string
}

export async function POST(request: NextRequest) {
  try {
    const authToken = resolveRequestAuthToken(request)
    if (!authToken) {
      return NextResponse.json({ error: 'Unauthorized' }, { status: 401 })
    }

    const formData = await request.formData()
    const file = formData.get('file')
    const merchantUserId = String(formData.get('merchantUserId') || '').trim()
    const merchantName = String(formData.get('merchantName') || '').trim()
    const merchantCustomId = String(formData.get('merchantCustomId') || '').trim()
    const consumerUserId = String(formData.get('consumerUserId') || '').trim()
    const consumerCustomId = String(formData.get('consumerCustomId') || '').trim()
    const consumerName = String(formData.get('consumerName') || '').trim()
    const consumerEmail = String(formData.get('consumerEmail') || '').trim()
    const billId = String(formData.get('billId') || '').trim()
    const vendor = String(formData.get('vendor') || '').trim()
    const purchaseDate = String(formData.get('purchaseDate') || '').trim()
    const totalAmount = String(formData.get('totalAmount') || '').trim()
    const requestedOcrMode = String(formData.get('ocrMode') || '').trim().toLowerCase()

    if (!(file instanceof File)) {
      return NextResponse.json({ error: 'No file provided' }, { status: 400 })
    }
    if (!merchantUserId || !consumerUserId) {
      return NextResponse.json(
        { error: 'merchantUserId and consumerUserId are required' },
        { status: 400 }
      )
    }

    const lowered = file.name.toLowerCase()
    const isPdf = lowered.endsWith('.pdf') || file.type === 'application/pdf'
    const isImage =
      file.type.startsWith('image/') || /\.(png|jpe?g|webp|bmp|tiff?)$/.test(lowered)
    if (!isPdf && !isImage) {
      return NextResponse.json(
        { error: 'Only PDF and image files are supported.' },
        { status: 400 }
      )
    }

    const backendFormData = new FormData()
    backendFormData.append('file', file, file.name)
    backendFormData.append('user_id', consumerUserId)
    backendFormData.append('merchant_user_id', merchantUserId)
    if (merchantName) backendFormData.append('merchant_name', merchantName)
    if (merchantCustomId) backendFormData.append('merchant_custom_id', merchantCustomId)
    if (consumerCustomId) backendFormData.append('consumer_custom_id', consumerCustomId)
    if (consumerName) backendFormData.append('consumer_name', consumerName)
    if (consumerEmail) backendFormData.append('consumer_email', consumerEmail)
    if (billId) backendFormData.append('bill_id', billId)
    if (vendor) backendFormData.append('vendor', vendor)
    if (purchaseDate) backendFormData.append('document_date', purchaseDate)
    if (totalAmount) backendFormData.append('total_amount', totalAmount)
    backendFormData.append('ocr_mode', requestedOcrMode || DEFAULT_OCR_MODE)

    const ingestPath = isPdf ? '/ingest/pdf' : '/ingest/image'
    const ingest = await backendApiFetch<IngestResponse>(ingestPath, {
      method: 'POST',
      body: backendFormData,
    }, INGEST_TIMEOUT_MS, authToken)

    const document = await backendApiFetch<unknown>(
      withQuery(`/documents/${ingest.document_id}`, { user_id: consumerUserId }),
      { method: 'GET' },
      DOCUMENT_FETCH_TIMEOUT_MS,
      authToken
    )

    return NextResponse.json({
      document,
      ingestion: ingest,
    })
  } catch (error) {
    if (error instanceof BackendApiError) {
      const payloadDetail =
        typeof error.payload === 'object' &&
        error.payload &&
        'detail' in error.payload &&
        typeof (error.payload as { detail?: unknown }).detail === 'string'
          ? String((error.payload as { detail: string }).detail)
          : null
      const resolvedMessage =
        payloadDetail ||
        (typeof error.payload === 'string' ? error.payload : null) ||
        error.message
      return NextResponse.json(
        { error: resolvedMessage },
        { status: error.status }
      )
    }
    const message = error instanceof Error ? error.message : 'Failed to upload and assign bill'
    return NextResponse.json({ error: message }, { status: 500 })
  }
}

import { NextRequest, NextResponse } from 'next/server'
import { BackendApiError, backendApiFetch, resolveRequestAuthToken, withQuery } from '@/lib/backend-api'

export const runtime = 'nodejs'
const INGEST_TIMEOUT_MS = 120000
const DOCUMENT_FETCH_TIMEOUT_MS = 60000
const OCR_TEXT_MIN_LENGTH = 1
const CLOUD_FAST_MODES = new Set(['auto', 'cloud', 'cloud_only', 'google', 'google_only', 'vision', 'vision_only'])
const HYBRID_CLOUD_MODES = new Set(['hybrid', 'cloud_hybrid', 'vision_bedrock', 'google_bedrock'])
const LOCAL_MODES = new Set(['local', 'local_only', 'tesseract', 'tesseractjs'])
const VISION_OCR_TIMEOUT_MS = 45000
const DEFAULT_OCR_MODE = 'hybrid'
const ASYNC_SCAN_ENABLED = String(process.env.SCAN_USE_ASYNC_PIPELINE || 'true').trim().toLowerCase() !== 'false'
const ASYNC_SCAN_POLL_INTERVAL_MS = 2500
const ASYNC_SCAN_INITIAL_WAIT_MS = 15000

interface IngestResponse {
  document_id: string
  chunk_count: number
  bill_id: string
  vendor: string
  created_at: string
}

interface AsyncExtractionJobCreateResponse {
  jobId: string
  status: string
  createdAt: string
}

interface AsyncExtractionJobStatusResponse {
  jobId: string
  status: string
  filename: string
  documentId?: string | null
  error?: string | null
  enginesUsed?: string[]
  createdAt: string
  updatedAt: string
  completedAt?: string | null
}

interface BackendWarrantyItem {
  productName?: string
  model?: string
  invoiceNo?: string
  purchaseDate?: string
  purchasePrice?: number
  quantity?: number
  unitPrice?: number
  gstAmount?: number
  warrantyMonths?: number
  warrantyStart?: string
  warrantyEnd?: string
  serialNumber?: string
}

interface ScanLineItem {
  name: string
  amount: string
  quantity?: number
  unitPrice?: number
  gstAmount?: number
}

interface BackendDocument {
  docId: string
  title: string
  rawText?: string
  sellerName?: string
  totalAmount?: number
  category?: string
  reviewRequired?: boolean
  lowConfidenceFields?: string[]
  taxableAmount?: number
  gstAmount?: number
  gstRate?: number
  cgstAmount?: number
  sgstAmount?: number
  igstAmount?: number
  items: BackendWarrantyItem[]
}

interface VisionOcrFields {
  invoiceNumber?: string | null
  invoiceDate?: string | null
  dueDate?: string | null
  totalAmount?: string | number | null
  gstin?: string | null
  vendorName?: string | null
  productName?: string | null
  poNumber?: string | null
}

interface VisionOcrResponse {
  ok?: boolean
  fullText?: string
  fields?: VisionOcrFields
}

function sanitizeProductLabel(value: string | null | undefined): string {
  const raw = String(value || '').trim()
  if (!raw) return ''
  let cleaned = raw.replace(/\s+/g, ' ').trim()
  cleaned = cleaned.replace(
    /\s+[0-9][0-9,]*(?:\.\d{1,2})?(?:\s+[0-9][0-9,]*(?:\.\d{1,2})?){1,6}\s*$/i,
    ''
  ).trim()
  cleaned = cleaned.replace(/^\d{4,}\s+/, '').trim()
  cleaned = cleaned.replace(/^[A-Z0-9-]*\/[A-Z0-9-]+\s+/i, '').trim()
  cleaned = cleaned.replace(/\s+(?:CN|IN|US|EU|UK)$/i, '').trim()
  if (!cleaned) return ''
  if (/^(customer|invoice|document|order|po|gst|pan|hsn|item)\s*(no|number|id|code)?$/i.test(cleaned)) return ''
  if (/customer number|invoice number|ship to|bill to|place of supply|gstin|address/i.test(cleaned)) return ''
  return cleaned.slice(0, 255)
}

function toIsoDate(value: string): string {
  const input = value.trim()
  if (!input) return ''
  if (/^\d{4}-\d{2}-\d{2}$/.test(input)) return input
  const dmy = input.match(/^(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})$/)
  if (dmy) {
    const day = Number.parseInt(dmy[1], 10)
    const month = Number.parseInt(dmy[2], 10)
    const yearRaw = Number.parseInt(dmy[3], 10)
    const year = yearRaw < 100 ? 2000 + yearRaw : yearRaw
    if (
      Number.isInteger(day) &&
      Number.isInteger(month) &&
      Number.isInteger(year) &&
      day >= 1 && day <= 31 &&
      month >= 1 && month <= 12 &&
      year >= 1900 && year <= 2100
    ) {
      return `${year.toString().padStart(4, '0')}-${month.toString().padStart(2, '0')}-${day.toString().padStart(2, '0')}`
    }
  }
  const ymd = input.match(/^(\d{4})[./-](\d{1,2})[./-](\d{1,2})$/)
  if (ymd) {
    const year = Number.parseInt(ymd[1], 10)
    const month = Number.parseInt(ymd[2], 10)
    const day = Number.parseInt(ymd[3], 10)
    if (
      Number.isInteger(day) &&
      Number.isInteger(month) &&
      Number.isInteger(year) &&
      day >= 1 && day <= 31 &&
      month >= 1 && month <= 12 &&
      year >= 1900 && year <= 2100
    ) {
      return `${year.toString().padStart(4, '0')}-${month.toString().padStart(2, '0')}-${day.toString().padStart(2, '0')}`
    }
  }
  const parsed = new Date(input)
  if (Number.isNaN(parsed.getTime())) return ''
  return parsed.toISOString().slice(0, 10)
}

function normalizeAmount(value: string | number | null | undefined): string {
  if (value === null || value === undefined) return ''
  const cleaned = String(value).replace(/[^0-9.,-]/g, '').replace(/,/g, '').trim()
  if (!cleaned) return ''
  const amount = Number.parseFloat(cleaned)
  if (!Number.isFinite(amount) || amount <= 0 || amount > 10000000) return ''
  return String(Number(amount.toFixed(2)))
}

async function extractWithVisionService(file: File): Promise<VisionOcrResponse | null> {
  const base = String(process.env.VISION_OCR_BASE_URL || 'http://127.0.0.1:8080').replace(/\/$/, '')
  const target = `${base}/api/ocr`
  const fd = new FormData()
  fd.append('file', file, file.name)
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), VISION_OCR_TIMEOUT_MS)
  try {
    const response = await fetch(target, {
      method: 'POST',
      body: fd,
      signal: controller.signal,
    })
    if (!response.ok) return null
    const payload = (await response.json().catch(() => null)) as VisionOcrResponse | null
    if (!payload?.ok) return null
    return payload
  } catch {
    return null
  } finally {
    clearTimeout(timer)
  }
}

function toScanPayload(document: BackendDocument, fileName: string) {
  const items: ScanLineItem[] = (document.items || [])
    .map((entry) => ({
      name: sanitizeProductLabel(entry.productName || ''),
      amount:
        entry.purchasePrice !== undefined && entry.purchasePrice !== null
          ? String(entry.purchasePrice)
          : '',
      quantity:
        entry.quantity !== undefined && entry.quantity !== null ? entry.quantity : undefined,
      unitPrice:
        entry.unitPrice !== undefined && entry.unitPrice !== null ? entry.unitPrice : undefined,
      gstAmount:
        entry.gstAmount !== undefined && entry.gstAmount !== null ? entry.gstAmount : undefined,
    }))
    .filter((entry) => entry.name || entry.amount)

  const item = document.items?.[0] || {}
  const fallbackProductName =
    sanitizeProductLabel(document.title || '') ||
    fileName.replace(/\.[^/.]+$/, '')
  const productName = sanitizeProductLabel(item.productName || '') || fallbackProductName
  const warrantyMonths = item.warrantyMonths
  const invoiceAmount =
    document.totalAmount !== undefined && document.totalAmount !== null
      ? String(document.totalAmount)
      : item.purchasePrice !== undefined && item.purchasePrice !== null
        ? String(item.purchasePrice)
        : ''
  return {
    docId: document.docId,
    title: fallbackProductName,
    fileName,
    reviewRequired: Boolean(document.reviewRequired),
    lowConfidenceFields: document.lowConfidenceFields || [],
    extractedText: document.rawText || '',
    details: {
      productName,
      brand: item.model || document.sellerName || '',
      category: document.category || 'Others',
      amount: invoiceAmount,
      purchaseDate: item.purchaseDate || '',
      warrantyPeriod: warrantyMonths ? `${warrantyMonths} Month(s)` : '',
      warrantyStart: item.warrantyStart || '',
      warrantyEnd: item.warrantyEnd || '',
      serialNumber: item.serialNumber || '',
      invoiceNumber: item.invoiceNo || '',
      store: document.sellerName || '',
      itemCount: items.length,
      items,
      gstAmount:
        item.gstAmount !== undefined && item.gstAmount !== null
          ? String(item.gstAmount)
          : document.gstAmount !== undefined && document.gstAmount !== null
            ? String(document.gstAmount)
            : '',
      gstRate: document.gstRate !== undefined && document.gstRate !== null ? String(document.gstRate) : '',
      taxableAmount: document.taxableAmount !== undefined && document.taxableAmount !== null ? String(document.taxableAmount) : '',
      cgstAmount: document.cgstAmount !== undefined && document.cgstAmount !== null ? String(document.cgstAmount) : '',
      sgstAmount: document.sgstAmount !== undefined && document.sgstAmount !== null ? String(document.sgstAmount) : '',
      igstAmount: document.igstAmount !== undefined && document.igstAmount !== null ? String(document.igstAmount) : '',
    },
  }
}

function sleep(ms: number) {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

async function fetchAsyncJobStatus(jobId: string, authToken?: string) {
  return backendApiFetch<AsyncExtractionJobStatusResponse>(
    `/extraction-jobs/${jobId}`,
    { method: 'GET' },
    DOCUMENT_FETCH_TIMEOUT_MS,
    authToken
  )
}

async function waitForAsyncJob(jobId: string, authToken: string | undefined, maxWaitMs: number) {
  const deadline = Date.now() + maxWaitMs
  let latest = await fetchAsyncJobStatus(jobId, authToken)
  while (Date.now() < deadline && !['completed', 'failed'].includes(String(latest.status || '').toLowerCase())) {
    await sleep(ASYNC_SCAN_POLL_INTERVAL_MS)
    latest = await fetchAsyncJobStatus(jobId, authToken)
  }
  return latest
}

async function fetchDocumentById(documentId: string, authToken?: string) {
  return backendApiFetch<BackendDocument>(
    withQuery(`/documents/${documentId}`, {}),
    { method: 'GET' },
    DOCUMENT_FETCH_TIMEOUT_MS,
    authToken
  )
}

export async function POST(request: NextRequest) {
  try {
    const authToken = resolveRequestAuthToken(request)
    const allowServiceTokenFallback =
      process.env.NODE_ENV !== 'production' ||
      String(process.env.ALLOW_SCAN_SERVICE_TOKEN_FALLBACK || '').trim().toLowerCase() === 'true'
    if (!authToken && !allowServiceTokenFallback) {
      return NextResponse.json({ error: 'Unauthorized' }, { status: 401 })
    }

    const formData = await request.formData()
    const file = formData.get('file')
    const rawUserId = String(formData.get('userId') || '').trim()
    const userId = rawUserId && rawUserId.toLowerCase() !== 'anonymous' ? rawUserId : ''
    const consumerEmail = String(formData.get('consumerEmail') || '').trim()
    const billId = String(formData.get('billId') || '').trim()
    const vendor = String(formData.get('vendor') || '').trim()
    const purchaseDate = String(formData.get('purchaseDate') || '').trim()
    const totalAmount = String(formData.get('totalAmount') || '').trim()
    const ocrText = String(formData.get('ocrText') || '').trim()
    const ocrMode = String(formData.get('ocrMode') || '').trim().toLowerCase()

    if (!(file instanceof File)) {
      return NextResponse.json({ error: 'No file provided' }, { status: 400 })
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

    const requestedOcrMode = ocrMode || DEFAULT_OCR_MODE
    const acceptedMode =
      CLOUD_FAST_MODES.has(requestedOcrMode) ||
      HYBRID_CLOUD_MODES.has(requestedOcrMode) ||
      LOCAL_MODES.has(requestedOcrMode)
    const effectiveOcrMode = acceptedMode
      ? requestedOcrMode
      : DEFAULT_OCR_MODE
    const normalizedManualPurchaseDate = purchaseDate ? toIsoDate(purchaseDate) : ''
    const normalizedManualTotalAmount = totalAmount ? normalizeAmount(totalAmount) : ''
    const shouldUseAsyncPipeline = ASYNC_SCAN_ENABLED && isImage && !LOCAL_MODES.has(effectiveOcrMode) && !ocrText
    let resolvedDocumentId = ''
    let asyncJobStatus: AsyncExtractionJobStatusResponse | null = null

    if (shouldUseAsyncPipeline) {
      const asyncFormData = new FormData()
      asyncFormData.append('file', file, file.name)
      if (userId) asyncFormData.append('user_id', userId)
      if (consumerEmail) asyncFormData.append('consumer_email', consumerEmail)
      asyncFormData.append('ocr_mode', effectiveOcrMode)
      if (billId) asyncFormData.append('bill_id', billId)
      if (vendor) asyncFormData.append('vendor', vendor)
      if (normalizedManualPurchaseDate) asyncFormData.append('document_date', normalizedManualPurchaseDate)
      if (normalizedManualTotalAmount) asyncFormData.append('total_amount', normalizedManualTotalAmount)

      try {
        const job = await backendApiFetch<AsyncExtractionJobCreateResponse>(
          '/extraction-jobs/image',
          {
            method: 'POST',
            body: asyncFormData,
          },
          INGEST_TIMEOUT_MS,
          authToken || undefined
        )
        asyncJobStatus = await waitForAsyncJob(job.jobId, authToken || undefined, ASYNC_SCAN_INITIAL_WAIT_MS)
        if (String(asyncJobStatus.status || '').toLowerCase() === 'completed' && asyncJobStatus.documentId) {
          resolvedDocumentId = asyncJobStatus.documentId
        } else if (String(asyncJobStatus.status || '').toLowerCase() === 'failed') {
          return NextResponse.json(
            { error: asyncJobStatus.error || 'Async extraction failed.' },
            { status: 422 }
          )
        } else {
          return NextResponse.json(
            {
              pending: true,
              jobId: job.jobId,
              status: asyncJobStatus.status || job.status,
            },
            { status: 202 }
          )
        }
      } catch (error) {
        if (!(error instanceof BackendApiError) || ![404, 405, 501, 503].includes(error.status)) {
          throw error
        }
      }
    }

    let ingest: IngestResponse | null = null
    let resolvedOcrText = ocrText
    if (!resolvedDocumentId) {
      const backendFormData = new FormData()
      backendFormData.append('file', file, file.name)
      if (userId) backendFormData.append('user_id', userId)
      if (consumerEmail) backendFormData.append('consumer_email', consumerEmail)
      backendFormData.append('ocr_mode', effectiveOcrMode)

      const shouldUseVisionHints =
        CLOUD_FAST_MODES.has(effectiveOcrMode) || HYBRID_CLOUD_MODES.has(effectiveOcrMode)
      if (shouldUseVisionHints) {
        const visionPayload = await extractWithVisionService(file)
        if (!resolvedOcrText) {
          const cloudText = String(visionPayload?.fullText || '').trim()
          if (cloudText.length >= OCR_TEXT_MIN_LENGTH) {
            resolvedOcrText = cloudText
          }
        }
      }

      if (billId) backendFormData.append('bill_id', billId)
      if (vendor) backendFormData.append('vendor', vendor)
      if (normalizedManualPurchaseDate) backendFormData.append('document_date', normalizedManualPurchaseDate)
      if (normalizedManualTotalAmount) backendFormData.append('total_amount', normalizedManualTotalAmount)

      if (resolvedOcrText) backendFormData.append('ocr_text', resolvedOcrText)

      const ingestPath = isPdf ? '/ingest/pdf' : '/ingest/image'
      ingest = await backendApiFetch<IngestResponse>(ingestPath, {
        method: 'POST',
        body: backendFormData,
      }, INGEST_TIMEOUT_MS, authToken || undefined)
      resolvedDocumentId = ingest.document_id
    }

    const document = await fetchDocumentById(
      resolvedDocumentId,
      authToken || undefined
    )

    const patchedDocument: BackendDocument = {
      ...document,
      items: Array.isArray(document.items) ? [...document.items] : [],
    }
    const manualBillIdProvided = billId.length > 0
    const manualVendorProvided = vendor.length > 0
    const manualPurchaseDateProvided = purchaseDate.length > 0
    const manualTotalAmountProvided = totalAmount.length > 0

    if (vendor && manualVendorProvided) patchedDocument.sellerName = vendor
    const firstItem: BackendWarrantyItem = patchedDocument.items[0] ? { ...patchedDocument.items[0] } : {}
    if (billId && manualBillIdProvided) firstItem.invoiceNo = billId
    if (normalizedManualPurchaseDate && manualPurchaseDateProvided) firstItem.purchaseDate = normalizedManualPurchaseDate
    if (normalizedManualTotalAmount && manualTotalAmountProvided) {
      const parsed = Number.parseFloat(normalizedManualTotalAmount)
      const current = firstItem.purchasePrice
      const hasWeakCurrent =
        current === undefined ||
        current === null ||
        !Number.isFinite(current) ||
        current <= 0 ||
        current > 10000000 ||
        (current >= 1900 && current <= 2100) ||
        (Number.isFinite(parsed) && parsed > 0 && (current < parsed * 0.4 || current > parsed * 3))
      if (Number.isFinite(parsed) && parsed > 0 && parsed <= 10000000 && hasWeakCurrent) {
        firstItem.purchasePrice = parsed
      }
    }
    if (patchedDocument.items.length > 0) {
      patchedDocument.items[0] = firstItem
    } else if (Object.keys(firstItem).length > 0) {
      patchedDocument.items = [firstItem]
    }

    return NextResponse.json({
      document: toScanPayload(patchedDocument, file.name),
      ingestion: ingest,
      asyncJob: asyncJobStatus,
    })
  } catch (error) {
    if (error instanceof BackendApiError) {
      return NextResponse.json(
        { error: error.payload || error.message },
        { status: error.status }
      )
    }
    const message = error instanceof Error ? error.message : 'Failed to process file'
    return NextResponse.json({ error: message }, { status: 500 })
  }
}

export async function GET(request: NextRequest) {
  try {
    const authToken = resolveRequestAuthToken(request)
    const jobId = String(request.nextUrl.searchParams.get('jobId') || '').trim()
    if (!jobId) {
      return NextResponse.json({ error: 'jobId is required' }, { status: 400 })
    }

    const status = await fetchAsyncJobStatus(jobId, authToken || undefined)
    const normalizedStatus = String(status.status || '').toLowerCase()
    if (normalizedStatus === 'failed') {
      return NextResponse.json(
        { error: status.error || 'Async extraction failed.' },
        { status: 422 }
      )
    }
    if (normalizedStatus !== 'completed' || !status.documentId) {
      return NextResponse.json(
        {
          pending: true,
          jobId: status.jobId,
          status: status.status,
        },
        { status: 202 }
      )
    }

    const document = await fetchDocumentById(status.documentId, authToken || undefined)
    return NextResponse.json({
      document: toScanPayload(document, status.filename || 'uploaded-image'),
      asyncJob: status,
    })
  } catch (error) {
    if (error instanceof BackendApiError) {
      return NextResponse.json(
        { error: error.payload || error.message },
        { status: error.status }
      )
    }
    const message = error instanceof Error ? error.message : 'Failed to fetch async scan status'
    return NextResponse.json({ error: message }, { status: 500 })
  }
}

import { NextResponse } from 'next/server'
import type { NextRequest } from 'next/server'

const ACCESS_TOKEN_COOKIE = 'sb_access_token'
const USER_TYPE_COOKIE = 'sb_user_type'

export async function POST(request: NextRequest) {
  try {
    const body = await request.json()
    const { token, userType } = body

    if (!token) {
      return NextResponse.json({ error: 'Missing token' }, { status: 400 })
    }

    const response = NextResponse.json({ ok: true })

    response.cookies.set(ACCESS_TOKEN_COOKIE, token, {
      httpOnly: true,
      secure: process.env.NODE_ENV === 'production',
      sameSite: 'lax',
      path: '/',
      maxAge: 60 * 60 * 24 * 7, // 7 days
    })

    if (userType) {
      response.cookies.set(USER_TYPE_COOKIE, userType, {
        httpOnly: false,
        secure: process.env.NODE_ENV === 'production',
        sameSite: 'lax',
        path: '/',
        maxAge: 60 * 60 * 24 * 7,
      })
    }

    return response
  } catch {
    return NextResponse.json({ error: 'Invalid request' }, { status: 400 })
  }
}

export async function DELETE() {
  const response = NextResponse.json({ ok: true })

  response.cookies.set(ACCESS_TOKEN_COOKIE, '', {
    httpOnly: true,
    secure: process.env.NODE_ENV === 'production',
    sameSite: 'lax',
    path: '/',
    maxAge: 0,
  })

  response.cookies.set(USER_TYPE_COOKIE, '', {
    httpOnly: false,
    secure: process.env.NODE_ENV === 'production',
    sameSite: 'lax',
    path: '/',
    maxAge: 0,
  })

  return response
}

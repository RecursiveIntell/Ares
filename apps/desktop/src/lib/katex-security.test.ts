import type { Element, Root } from 'hast'
import katex from 'katex'
import { unified } from 'unified'
import { VFile } from 'vfile'
import { describe, expect, it } from 'vitest'

import { createMemoizedMathPlugin } from './katex-memo'

function render(expression: string, display = false): Root {
  const tree: Root = {
    type: 'root',
    children: [
      {
        type: 'element',
        tagName: 'p',
        properties: {},
        children: [
          {
            type: 'element',
            tagName: 'code',
            properties: { className: [display ? 'math-display' : 'math-inline'] },
            children: [{ type: 'text', value: expression }]
          }
        ]
      }
    ]
  }

  return unified().use([createMemoizedMathPlugin().rehypePlugin]).runSync(tree, new VFile()) as Root
}

function elements(tree: Root): Element[] {
  const result: Element[] = []

  function visit(node: Root | Root['children'][number]) {
    if (node.type === 'element') {
      result.push(node)
    }

    if ('children' in node) {
      for (const child of node.children) {
        visit(child)
      }
    }
  }

  visit(tree)

  return result
}

function withInheritedTrust(callback: () => void) {
  const previous = Object.getOwnPropertyDescriptor(Object.prototype, 'trust')
  Object.defineProperty(Object.prototype, 'trust', { value: true, configurable: true, writable: true })

  try {
    callback()
  } finally {
    if (previous) {
      Object.defineProperty(Object.prototype, 'trust', previous)
    } else {
      Reflect.deleteProperty(Object.prototype, 'trust')
    }
  }
}

describe('real KaTeX security migration', () => {
  it.each(['x^2 + y^2', '\\frac{1}{2}', '\\sqrt{a+b}', '\\begin{pmatrix}a&b\\\\c&d\\end{pmatrix}'])(
    'keeps supported math rendering: %s',
    expression => {
      const nodes = elements(render(expression))
      expect(
        nodes.some(node => Array.isArray(node.properties.className) && node.properties.className.includes('katex'))
      ).toBe(true)
      expect(nodes.some(node => node.tagName === 'math')).toBe(true)
    }
  )

  it('ignores inherited trust in the real renderer (GHSA-238p-pmpm-9mq7)', () => {
    withInheritedTrust(() => {
      const html = katex.renderToString('\\href{javascript:alert(1)}{click}', { throwOnError: false })
      expect(html).not.toMatch(/<a\b[^>]*href=["']javascript:/i)
    })
  })

  it('keeps inherited trust out of memoized chat math output too', () => {
    withInheritedTrust(() => {
      const nodes = elements(render('\\href{javascript:alert(2)}{click}'))
      expect(
        nodes.filter(node => node.tagName === 'a' && String(node.properties.href).startsWith('javascript:'))
      ).toEqual([])
    })
  })

  it('retains fallback text without generating active markup', () => {
    const tree = render('\\notARealCommand{<img src=x onerror=alert(1)>}')
    expect(elements(tree).some(node => node.tagName === 'img' || 'onerror' in node.properties)).toBe(false)
  })
})

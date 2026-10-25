import type {ReactNode} from 'react';
import Link from '@docusaurus/Link';
import {useCurrentSidebarCategory} from '@docusaurus/plugin-content-docs/client';
import type {PropSidebarItem} from '@docusaurus/plugin-content-docs';

function Items({items}: {items: PropSidebarItem[]}): ReactNode {
  return (
    <ul>
      {items.map((item) => {
        if (item.type === 'html') {
          return null;
        }
        if (item.type === 'category') {
          return (
            <li key={item.label}>
              {item.href ? <Link to={item.href}>{item.label}</Link> : item.label}
              <Items items={item.items} />
            </li>
          );
        }
        return (
          <li key={item.href}>
            <Link to={item.href}>{item.label}</Link>
          </li>
        );
      })}
    </ul>
  );
}

/** Plain bulleted list of the pages in the current sidebar category. */
export default function DocList(): ReactNode {
  const category = useCurrentSidebarCategory();
  return <Items items={category.items} />;
}

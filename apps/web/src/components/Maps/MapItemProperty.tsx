import { FC } from 'react';

type MapItemPropertyProps = {
  layerName: string;
  itemName?: string;
  severity: number;
};

const MapItemProperty: FC<MapItemPropertyProps> = ({ layerName, itemName, severity }) => (
  <div className="bg-white rounded text-xs px-1 py-0.5 w-fit">
    <strong>
      {layerName}
      {itemName && ` - ${itemName}`}
    </strong>
    <small className="ml-1">Severity: {severity}</small>
  </div>
);

export default MapItemProperty;
